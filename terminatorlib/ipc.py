# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""ipc.py - DBus server and API calls"""

import os
import re
import sys
import json
import hashlib
import shlex
import time
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Vte', '2.91')
from gi.repository import Gdk
from gi.repository import GdkPixbuf
from gi.repository import Vte
import dbus.service
from dbus.exceptions import DBusException
import dbus.glib
import psutil
from .borg import Borg
from .terminator import Terminator
from .config import Config
from .factory import Factory
from .util import dbg, err, enumerate_descendants, widget_pixbuf
from .terminal import Terminal
from .container import Container
from .configjson import ConfigJson
from gi.repository import Gtk as gtk
from gi.repository import GObject as gobject
from .agent_control import (
    AgentControlError,
    as_bool,
    audit_event,
    contains_busy_marker,
    decode_key_escapes,
    echo_needle,
    iso_timestamp,
    json_error,
    json_result,
    process_waits_for_input,
    text_metadata,
)

CONFIG = Config()
if not CONFIG['dbus']:
    # The config says we are not to load dbus, so pretend like we can't
    dbg('dbus disabled')
    raise ImportError

BUS_BASE = 'net.tenshu.Terminator2'
BUS_PATH = '/net/tenshu/Terminator2'
MAX_SCROLLSHOT_PIXELS = 250000000
try:
    # Try and include the X11 display name in the dbus bus name
    DISPLAY = Gdk.get_display().partition('.')[0]
    # In Python 3, hash() uses a different seed on each run, so use hashlib
    DISPLAY = hashlib.md5(DISPLAY.encode('utf-8')).hexdigest()
    BUS_NAME = '%s%s' % (BUS_BASE, DISPLAY)
except:
    BUS_NAME = BUS_BASE

def pad_pixbuf_to_ratio(pixbuf, ratio_w=16, ratio_h=9):
    """Letterbox a pixbuf to the given aspect ratio with black bars,
    without scaling or cropping the original image"""
    width, height = pixbuf.get_width(), pixbuf.get_height()
    if width * ratio_h >= height * ratio_w:
        new_w = width
        new_h = (width * ratio_h + ratio_w - 1) // ratio_w
    else:
        new_h = height
        new_w = (height * ratio_w + ratio_h - 1) // ratio_h
    if (new_w, new_h) == (width, height):
        return pixbuf
    out = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8,
                               new_w, new_h)
    out.fill(0x000000ff)
    pixbuf.copy_area(0, 0, width, height, out,
                     (new_w - width) // 2, (new_h - height) // 2)
    return out


def rect_to_dict(rect):
    """Convert a Gdk.Rectangle-like object into a JSON-friendly dict."""
    return {
        'x': int(rect.x),
        'y': int(rect.y),
        'width': int(rect.width),
        'height': int(rect.height),
    }


def rect_fully_inside(inner, outer):
    """Return True when inner is completely contained in outer."""
    return (
        inner.x >= outer.x and
        inner.y >= outer.y and
        inner.x + inner.width <= outer.x + outer.width and
        inner.y + inner.height <= outer.y + outer.height
    )


def gdk_window_rect(gdk_window):
    """Return a Gdk.Rectangle for a GdkWindow in root-window coordinates."""
    _, x, y = gdk_window.get_origin()
    rect = Gdk.Rectangle()
    rect.x = int(x)
    rect.y = int(y)
    rect.width = int(gdk_window.get_width())
    rect.height = int(gdk_window.get_height())
    return rect


def pump_gtk_events(duration=0.0):
    """Let VTE process PTY input while servicing a synchronous D-Bus call."""
    deadline = time.monotonic() + max(0.0, float(duration))
    while True:
        while gtk.events_pending():
            gtk.main_iteration_do(False)
        if time.monotonic() >= deadline:
            break
        time.sleep(min(0.02, deadline - time.monotonic()))


def terminal_process_info(terminal):
    """Return shell and foreground process details without shelling out."""
    shell_pid = int(terminal.pid or 0)
    foreground_pid = shell_pid
    try:
        pty = terminal.get_vte().get_pty()
        if pty is not None:
            foreground_pid = int(os.tcgetpgrp(pty.get_fd()))
    except (AttributeError, OSError, ValueError):
        pass
    result = {
        'shell_pid': shell_pid or None,
        'foreground_pid': foreground_pid or None,
        'foreground_process': None,
        'foreground_argv': [],
    }
    if foreground_pid:
        try:
            process = psutil.Process(foreground_pid)
            result['foreground_process'] = process.name()
            result['foreground_argv'] = process.cmdline()
        except (psutil.Error, OSError):
            pass
    return result


def terminal_activity_state(terminal, process_info=None, text=None):
    """Classify current activity using output recency and foreground state."""
    process_info = process_info or terminal_process_info(terminal)
    if terminal.is_held_open:
        return 'exited'
    shell_pid = process_info.get('shell_pid')
    if shell_pid:
        try:
            if not psutil.pid_exists(shell_pid):
                return 'exited'
        except (psutil.Error, OSError):
            pass
    text = terminal.get_text() if text is None else text
    if contains_busy_marker(text):
        return 'busy'
    if time.monotonic() - terminal.last_activity_monotonic < 1.5:
        return 'busy'
    foreground_pid = process_info.get('foreground_pid')
    if foreground_pid and shell_pid and foreground_pid == shell_pid:
        return 'idle'
    if foreground_pid and process_waits_for_input(
            process_info.get('foreground_process'),
            process_info.get('foreground_argv')):
        return 'waiting_input'
    if foreground_pid:
        return 'busy'
    return 'unknown'


class DBusService(Borg, dbus.service.Object):
    """DBus Server class. This is implemented as a Borg"""
    bus_name = None
    bus_path = None
    terminator = None
    send_sequence = 0
    terminal_leases = None

    def __init__(self):
        """Class initialiser"""
        Borg.__init__(self, self.__class__.__name__)
        self.prepare_attributes()
        dbus.service.Object.__init__(self, self.bus_name, BUS_PATH)

    def prepare_attributes(self):
        """Ensure we are populated"""
        if self.terminal_leases is None:
            self.terminal_leases = {}
        if not self.bus_name:
            dbg('Checking for bus name availability: %s' % BUS_NAME)
            try:
                bus = dbus.SessionBus()
            except Exception as e:
                err('Unable to connect to DBUS Server, proceeding as standalone')
                raise ImportError
            proxy = bus.get_object('org.freedesktop.DBus',
                                   '/org/freedesktop/DBus')
            flags = 1 | 4 # allow replacement | do not queue
            if not proxy.RequestName(BUS_NAME, dbus.UInt32(flags)) in (1, 4):
                dbg('bus name unavailable: %s' % BUS_NAME)
                raise dbus.exceptions.DBusException(
                    "Couldn't get DBus name %s: Name exists" % BUS_NAME)
            self.bus_name = dbus.service.BusName(BUS_NAME,
                                                 bus=dbus.SessionBus())
        if not self.bus_path:
            self.bus_path = BUS_PATH
        if not self.terminator:
            self.terminator = Terminator()

    def _find_terminal(self, uuid=None, label=None):
        """Resolve a terminal by UUID or a unique stable label."""
        if uuid:
            terminal = self.terminator.find_terminal_by_uuid(str(uuid))
            if not terminal:
                raise AgentControlError(
                    'TERMINAL_NOT_FOUND',
                    'terminal with supplied UUID was not found')
            return terminal
        if label:
            matches = [terminal for terminal in self.terminator.terminals
                       if terminal.agent_label == str(label)]
            if not matches:
                raise AgentControlError(
                    'TERMINAL_NOT_FOUND',
                    'no terminal has label %s' % label)
            if len(matches) > 1:
                raise AgentControlError(
                    'AMBIGUOUS_LABEL',
                    'label %s matches multiple terminals' % label,
                    matches=[terminal.uuid.urn for terminal in matches])
            return matches[0]
        raise AgentControlError(
            'TERMINAL_NOT_FOUND', 'supply --uuid or --label')

    def _tab_context(self, terminal):
        """Return the stable tab UUID/title for a terminal, if tabbed."""
        maker = Factory()
        window = terminal.get_toplevel()
        root_widget = window.get_children()[0]
        if not maker.isinstance(root_widget, 'Notebook'):
            return None, None
        for tab_child in root_widget.get_children():
            terms = [tab_child]
            if not maker.isinstance(tab_child, 'Terminal'):
                terms = enumerate_descendants(tab_child)[1]
            if terminal in terms:
                label = root_widget.get_tab_label(tab_child)
                tab_uuid = getattr(getattr(label, 'uuid', None), 'urn', None)
                return tab_uuid, label.get_label()
        return None, None

    def _check_write_lease(self, terminal, owner=None):
        now = time.monotonic()
        uuid = terminal.uuid.urn
        lease = self.terminal_leases.get(uuid)
        if lease and lease['expires_at_monotonic'] <= now:
            del self.terminal_leases[uuid]
            lease = None
        if lease and lease['owner'] != owner:
            raise AgentControlError(
                'SESSION_LEASED', 'terminal is leased by another owner',
                retryable=True, owner=lease['owner'],
                expires_at=lease['expires_at'])

    def _terminal_identity(self, terminal):
        process_info = terminal_process_info(terminal)
        tab_uuid, tab_title = self._tab_context(terminal)
        window = terminal.get_toplevel()
        visible_text = terminal.get_text()
        return {
            'terminal_uuid': terminal.uuid.urn,
            'agent_label': terminal.agent_label,
            'session_title': terminal.get_session_title(),
            'tab_uuid': tab_uuid,
            'tab_title': tab_title,
            'window_uuid': window.uuid.urn,
            'window_title': window.get_title(),
            'cwd': terminal.get_cwd(),
            'last_activity_at': iso_timestamp(terminal.last_activity_at),
            'activity_state': terminal_activity_state(
                terminal, process_info, visible_text),
            'screen_revision': terminal.screen_revision,
            'screen_mode': 'unknown',
            **process_info,
        }

    @dbus.service.method(BUS_NAME, in_signature='a{ss}')
    def new_window_cmdline(self, options=dbus.Dictionary()):
        """Create a new Window"""
        dbg('dbus method called: new_window with parameters %s'%(options))
        if options['configjson']:
            dbg(options['configjson'])
            configjson = ConfigJson()
            layoutname = configjson.extend_config(options['configjson'])
            if layoutname and ((not options['layout']) or options['layout'] == 'default'):
                options['layout'] = layoutname
                if not options['profile']:
                    options['profile'] = configjson.get_profile_to_use()

        oldopts = self.terminator.config.options_get()
        oldopts.__dict__ = options
        self.terminator.config.options_set(oldopts)
        self.terminator.create_layout(oldopts.layout)
        self.terminator.layout_done()

    @dbus.service.method(BUS_NAME, in_signature='a{ss}')
    def new_tab_cmdline(self, options=dbus.Dictionary()):
        """Create a new tab"""
        dbg('dbus method called: new_tab with parameters %s'%(options))
        oldopts = self.terminator.config.options_get()
        oldopts.__dict__ = options
        self.terminator.config.options_set(oldopts)
        window = self.terminator.get_windows()[0]
        window.tab_new()

    @dbus.service.method(BUS_NAME, in_signature='a{ss}')
    def toggle_visibility_cmdline(self,options=dbus.Dictionary):
        dbg('toggle_visibility_cmdline')
        for window in self.terminator.get_windows():
            window.on_hide_window()

    @dbus.service.method(BUS_NAME, in_signature='a{ss}')
    def unhide_cmdline(self,options=dbus.Dictionary):
        dbg('unhide_cmdline')
        for window in self.terminator.get_windows():
            if not window.get_property('visible'):
                window.on_hide_window()

    @dbus.service.method(BUS_NAME)
    def new_window(self):
        """Create a new Window"""
        terminals_before = set(self.get_terminals())
        self.terminator.new_window()
        terminals_after = set(self.get_terminals())
        new_terminal_set = list(terminals_after - terminals_before)
        if len(new_terminal_set) != 1:
            return "ERROR: Cannot determine the UUID of the added terminal"
        else:
            return new_terminal_set[0]

    @dbus.service.method(BUS_NAME)
    def new_tab(self, uuid=None):
        """Create a new tab"""
        return self.new_terminal(uuid, 'tab')

    @dbus.service.method(BUS_NAME)
    def reload_configuration(self):
        """Reload configuration for all terminals"""
        self.terminator.config.base.reload()
        self.terminator.reconfigure()

    @dbus.service.method(BUS_NAME) 
    def bg_img_all (self,options=dbus.Dictionary()):
        for terminal in self.terminator.terminals:
            terminal.set_background_image(options.get('file')) 
            
    @dbus.service.method(BUS_NAME) 
    def bg_img(self,uuid=None,options=dbus.Dictionary()):
        self.terminator.find_terminal_by_uuid(uuid).set_background_image(options.get('file'))

    @dbus.service.method(BUS_NAME)
    def hsplit(self, uuid=None,options=None):
        """Split a terminal horizontally, by UUID"""
        if options:
            cmd = options.get('execute')
            title = options.get('title')
            return self.new_terminal_cmd(uuid=uuid, title=title, cmd=cmd, split_vert=True) 
        else:
            return self.new_terminal(uuid, 'hsplit')

    @dbus.service.method(BUS_NAME)
    def vsplit(self, uuid=None,options=None):
        """Split a terminal vertically, by UUID"""
        if options:
            cmd = options.get('execute')
            title = options.get('title')
            return self.new_terminal_cmd(uuid=uuid, title=title, cmd=cmd, split_vert=False) 
        else:
            return self.new_terminal(uuid, 'vsplit')

    def _split_terminal(self, terminal, options):
        """Create a configured sibling and return it plus split metadata."""
        orientation = str(options.get('orientation', 'vertical')).lower()
        side = str(options.get('side', '')).lower()
        if orientation not in ('horizontal', 'vertical'):
            raise AgentControlError(
                'INVALID_LAYOUT',
                'orientation must be horizontal or vertical')
        allowed_sides = {
            'horizontal': ('top', 'bottom'),
            'vertical': ('left', 'right'),
        }
        if not side:
            side = allowed_sides[orientation][1]
        if side not in allowed_sides[orientation]:
            raise AgentControlError(
                'INVALID_LAYOUT',
                'side %s is invalid for %s orientation' %
                (side, orientation))
        try:
            ratio = float(options.get('ratio', 0.5))
        except (TypeError, ValueError):
            raise AgentControlError(
                'INVALID_LAYOUT', 'ratio must be a number from 0.1 to 0.9')
        if ratio < 0.1 or ratio > 0.9:
            raise AgentControlError(
                'INVALID_LAYOUT', 'ratio must be from 0.1 to 0.9')
        cwd = str(options.get('cwd') or terminal.get_cwd())
        if not os.path.isdir(os.path.expanduser(cwd)):
            raise AgentControlError(
                'INVALID_CWD', 'directory does not exist: %s' % cwd)
        profile = options.get('profile')
        if profile and profile not in terminal.config.list_profiles():
            raise AgentControlError(
                'INVALID_PROFILE', 'profile does not exist: %s' % profile)
        maker = Factory()
        sibling = maker.make('Terminal')
        sibling.set_cwd(cwd)
        if profile:
            sibling.force_set_profile(None, profile)
        label = str(options.get('label', '')).strip()
        if label:
            sibling.set_agent_label(label)
        command = options.get('execute')
        sibling.spawn_child(init_command=command)
        container = self.get_terminal_container(terminal)
        sibling_first = side in ('left', 'top')
        # Terminator calls top/bottom a VPaned (vertical=True) and
        # left/right an HPaned (vertical=False).
        vertical = orientation == 'horizontal'
        container.split_axis(
            terminal, vertical, cwd, sibling,
            widgetfirst=not sibling_first)
        pump_gtk_events(0.05)
        paned = sibling.get_parent()
        if hasattr(paned, 'ratio'):
            # Paned.ratio is the first child's share; the API ratio is the new
            # pane's share regardless of side.
            paned.ratio = ratio if sibling_first else 1.0 - ratio
            paned.set_position_by_ratio()
        if not as_bool(options.get('focus'), True):
            terminal.grab_focus()
        return sibling, {
            'source_terminal_uuid': terminal.uuid.urn,
            'new_terminal_uuid': sibling.uuid.urn,
            'orientation': orientation,
            'side': side,
            'ratio': ratio,
            'layout_revision': time.time_ns(),
        }

    @dbus.service.method(BUS_NAME)
    def split(self, uuid=None, options=dbus.Dictionary()):
        """Atomically split a pane with placement, ratio and child options."""
        try:
            terminal = self._find_terminal(uuid=uuid)
            sibling, result = self._split_terminal(terminal, options)
            identity = self._terminal_identity(sibling)
            result.update({
                'new_session_id': None,
                'tab_uuid': identity['tab_uuid'],
                'agent_label': identity['agent_label'],
                'cwd': identity['cwd'],
            })
            audit_event('split', **result)
            return json_result(**result)
        except AgentControlError as ex:
            return json.dumps(ex.as_dict(), ensure_ascii=False, sort_keys=True)
        except Exception as ex:
            return json_error('INVALID_LAYOUT', str(ex))

    @dbus.service.method(BUS_NAME)
    def attach_headless(self, uuid=None, options=dbus.Dictionary()):
        """Attach a tmux-backed headless session to a new tab or split."""
        backend_id = str(options.get('backend_id', ''))
        if not re.fullmatch(r'terminator-agent-[0-9a-f-]{32,36}', backend_id):
            return json_error(
                'SESSION_NOT_FOUND', 'invalid headless backend identity')
        try:
            terminal = self._find_terminal(uuid=uuid)
            command = 'exec tmux attach-session -t %s' % shlex.quote(backend_id)
            attach_options = dict(options)
            attach_options['execute'] = command
            mode = str(options.get('attach_mode', 'new-tab'))
            if mode == 'new-tab':
                maker = Factory()
                sibling = maker.make('Terminal')
                sibling.set_cwd(str(options.get('cwd') or terminal.get_cwd()))
                label = str(options.get('label', '')).strip()
                if label:
                    sibling.set_agent_label(label)
                sibling.spawn_child(init_command=command)
                window = terminal.get_toplevel()
                if not window.is_child_notebook():
                    maker.make('Notebook', window=window)
                window.get_child().newtab(widget=sibling)
                pump_gtk_events(0.05)
                result = {
                    'source_terminal_uuid': terminal.uuid.urn,
                    'new_terminal_uuid': sibling.uuid.urn,
                    'attach_mode': mode,
                }
            else:
                side_map = {
                    'split-left': ('vertical', 'left'),
                    'split-right': ('vertical', 'right'),
                    'split-top': ('horizontal', 'top'),
                    'split-bottom': ('horizontal', 'bottom'),
                }
                if mode not in side_map:
                    raise AgentControlError(
                        'INVALID_LAYOUT', 'invalid attach mode: %s' % mode)
                attach_options['orientation'], attach_options['side'] = \
                    side_map[mode]
                sibling, result = self._split_terminal(
                    terminal, attach_options)
                result['attach_mode'] = mode
            result['backend_id'] = backend_id
            audit_event('attach_session', **result)
            return json_result(**result)
        except AgentControlError as ex:
            return json.dumps(ex.as_dict(), ensure_ascii=False, sort_keys=True)
        except Exception as ex:
            return json_error('INVALID_LAYOUT', str(ex))

    def get_terminal_container(self, terminal, container=None):
        terminator = Terminator()
        if not container:
            for window in terminator.windows:
                owner = self.get_terminal_container(terminal, window)
                if owner: return owner
        else:
            for child in container.get_children():
                if isinstance(child, Terminal) and child == terminal:
                    return container
                if isinstance(child, Container):
                    owner = self.get_terminal_container(terminal, child)
                    if owner: return owner

    def new_terminal_cmd(self, uuid=None, title=None, cmd=None, split_vert=False):
        """Split a terminal by UUID and immediately runs the specified command in the new terminal"""
        if not uuid:
            return "ERROR: No UUID specified"

        terminal = self.terminator.find_terminal_by_uuid(uuid)

        terminals_before = set(self.get_terminals())
        if not terminal:
            return "ERROR: Terminal with supplied UUID not found"

        # get current working dir out of target terminal
        cwd = terminal.get_cwd()

        # get current container
        container = self.get_terminal_container(terminal)
        maker = Factory()
        sibling = maker.make('Terminal')
        sibling.set_cwd(cwd)
        if title: sibling.titlebar.set_custom_string(title)
        sibling.spawn_child(init_command=cmd)

        # split and run command in new terminal
        container.split_axis(terminal, split_vert, cwd, sibling)

        terminals_after = set(self.get_terminals())
        # Detect the new terminal UUID
        new_terminal_set = list(terminals_after - terminals_before)
        if len(new_terminal_set) != 1:
            return "ERROR: Cannot determine the UUID of the added terminal"
        else:
            return new_terminal_set[0]

    def new_terminal(self, uuid, type):
        """Split a terminal horizontally o?r vertically, by UUID"""
        dbg('dbus method called: %s' % type)
        if not uuid:
            return "ERROR: No UUID specified"
        terminal = self.terminator.find_terminal_by_uuid(uuid)
        terminals_before = set(self.get_terminals())
        if not terminal:
            return "ERROR: Terminal with supplied UUID not found"
        elif type == 'tab':
            terminal.key_new_tab()
        elif type == 'hsplit':
            terminal.key_split_horiz()
        elif type == 'vsplit':
            terminal.key_split_vert()
        else:
            return "ERROR: Unknown type \"%s\" specified" % (type)
        terminals_after = set(self.get_terminals())
        # Detect the new terminal UUID
        new_terminal_set = list(terminals_after - terminals_before)
        if len(new_terminal_set) != 1:
            return "ERROR: Cannot determine the UUID of the added terminal"
        else:
            return new_terminal_set[0]

    @dbus.service.method(BUS_NAME)
    def get_terminals(self):
        """Return a list of all the terminals"""
        return [x.uuid.urn for x in self.terminator.terminals]

    @dbus.service.method(BUS_NAME)
    def get_focused_terminal(self):
        """Returns the uuid of the currently focused terminal"""
        if self.terminator.last_focused_term:
            return self.terminator.last_focused_term.uuid.urn
        return None

    @dbus.service.method(BUS_NAME)
    def get_window(self, uuid=None):
        """Return the UUID of the parent window of a given terminal"""
        terminal = self.terminator.find_terminal_by_uuid(uuid)
        window = terminal.get_toplevel()
        return window.uuid.urn

    @dbus.service.method(BUS_NAME)
    def get_window_title(self, uuid=None):
        """Return the title of a parent window of a given terminal"""
        terminal = self.terminator.find_terminal_by_uuid(uuid)
        window = terminal.get_toplevel()
        return window.get_title()

    @dbus.service.method(BUS_NAME)
    def get_tab(self, uuid=None):
        """Return the UUID of the parent tab of a given terminal"""
        terminal = self.terminator.find_terminal_by_uuid(uuid)
        if not terminal:
            return "ERROR: Terminal with supplied UUID not found"
        return self._tab_context(terminal)[0] or ''

    @dbus.service.method(BUS_NAME)
    def get_tab_title(self, uuid=None):
        """Return the title of a parent tab of a given terminal"""
        terminal = self.terminator.find_terminal_by_uuid(uuid)
        if not terminal:
            return "ERROR: Terminal with supplied UUID not found"
        return self._tab_context(terminal)[1] or ''

    @dbus.service.method(BUS_NAME)
    def get_terminal_title(self, uuid=None):
        """Return stable and dynamic title layers as JSON."""
        try:
            terminal = self._find_terminal(uuid=uuid)
            identity = self._terminal_identity(terminal)
            fields = {
                key: identity[key]
                for key in (
                    'terminal_uuid', 'agent_label', 'session_title',
                    'tab_uuid', 'tab_title', 'window_uuid', 'window_title',
                    'foreground_process',
                )
            }
            return json_result(**fields)
        except AgentControlError as ex:
            return json.dumps(ex.as_dict(), ensure_ascii=False, sort_keys=True)

    @dbus.service.method(BUS_NAME)
    def set_terminal_label(self, uuid=None, options=dbus.Dictionary()):
        """Set a stable pane label that OSC title changes cannot overwrite."""
        try:
            terminal = self._find_terminal(uuid=uuid)
            label = str(options.get('label', '')).strip()
            if not label:
                return json_error('INVALID_LABEL', 'label must not be empty')
            terminal.set_agent_label(label)
            audit_event('set_terminal_label', terminal_uuid=uuid, label=label)
            return json_result(
                terminal_uuid=uuid, agent_label=terminal.agent_label)
        except AgentControlError as ex:
            return json.dumps(ex.as_dict(), ensure_ascii=False, sort_keys=True)

    @dbus.service.method(BUS_NAME)
    def clear_terminal_label(self, uuid=None,
                             options=dbus.Dictionary()):
        """Clear a terminal's stable agent label."""
        try:
            terminal = self._find_terminal(uuid=uuid)
            terminal.set_agent_label(None)
            audit_event('clear_terminal_label', terminal_uuid=uuid)
            return json_result(terminal_uuid=uuid, agent_label=None)
        except AgentControlError as ex:
            return json.dumps(ex.as_dict(), ensure_ascii=False, sort_keys=True)

    @dbus.service.method(BUS_NAME)
    def list_terminals(self, options=dbus.Dictionary()):
        """Return all pane identities in one structured call."""
        terminals = [
            self._terminal_identity(terminal)
            for terminal in self.terminator.terminals
        ]
        labels = {}
        for terminal in terminals:
            label = terminal.get('agent_label')
            if label:
                labels.setdefault(label, []).append(
                    terminal['terminal_uuid'])
        warnings = [
            {
                'code': 'DUPLICATE_LABEL',
                'label': label,
                'matches': matches,
            }
            for label, matches in sorted(labels.items())
            if len(matches) > 1
        ]
        return json_result(terminals=terminals, warnings=warnings)

    @dbus.service.method(BUS_NAME)
    def set_tab_title(self, uuid=None, options=dbus.Dictionary()):
        """Set the title of a parent tab of a given terminal"""
        tab_title = options.get('tab-title')

        maker = Factory()
        terminal = self.terminator.find_terminal_by_uuid(uuid)
        window = terminal.get_toplevel()

        if not window.is_child_notebook():
            return

        notebook = window.get_children()[0]
        n_page = notebook.get_current_page()
        page = notebook.get_nth_page(n_page)
        label = notebook.get_tab_label(page)
        label.set_custom_label(tab_title, force=True)

    @dbus.service.method(BUS_NAME)
    def switch_profile(self, uuid=None, options=dbus.Dictionary()):
        """Switch profile of a given terminal"""
        terminal = self.terminator.find_terminal_by_uuid(uuid)
        profile_name = options.get('profile')
        terminal.force_set_profile(False, profile_name)

    @dbus.service.method(BUS_NAME)
    def switch_profile_all(self, options=dbus.Dictionary()):
        """Switch profile of a given terminal"""
        for terminal in self.terminator.terminals:
            profile_name = options.get('profile')
            terminal.force_set_profile(False, profile_name)

    @dbus.service.method(BUS_NAME)
    def feed_terminal(self, uuid=None, options=dbus.Dictionary()):
        """Feed text to the terminal with the given UUID, as if typed"""
        terminal = self.terminator.find_terminal_by_uuid(uuid)
        if not terminal:
            return "ERROR: Terminal with supplied UUID not found"
        text = options.get('text')
        if text is None:
            return "ERROR: No text supplied (use --text)"
        if as_bool(options.get('enter')):
            text = str(text) + '\r'
        terminal.feed(str(text))
        return "OK"

    @dbus.service.method(BUS_NAME)
    def send(self, options=dbus.Dictionary()):
        """Atomically type text, optionally verify echo, and send Enter."""
        try:
            terminal = self._find_terminal(
                uuid=options.get('uuid'), label=options.get('label'))
            owner = options.get('owner')
            self._check_write_lease(terminal, owner)
            if 'text' not in options:
                return json_error('NO_TEXT', 'no text supplied (use --text)')
            text = str(options.get('text', ''))
            submit = as_bool(options.get('submit'))
            verify_echo = as_bool(options.get('verify_echo'))
            try:
                wait_busy = max(0.0, float(options.get('wait_busy', 0)))
            except (TypeError, ValueError):
                return json_error(
                    'INVALID_TIMEOUT', '--wait-busy must be a number')

            if terminal.screen_revision == 0:
                terminal._record_screen_change()
            revision_before = terminal.screen_revision
            state_before = terminal_activity_state(terminal)
            terminal.feed(text)

            echo_observed = not verify_echo
            if verify_echo:
                needle = echo_needle(text)
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    pump_gtk_events(0.03)
                    footer = '\n'.join(
                        terminal.get_text().splitlines()[-32:])
                    haystack = re.sub(r'\s+', '', footer)
                    if (not needle or
                            (terminal.screen_revision > revision_before and
                             needle in haystack)):
                        echo_observed = True
                        break

            enter_sent = False
            revision_before_enter = terminal.screen_revision
            if submit and echo_observed:
                terminal.feed('\r')
                enter_sent = True
                pump_gtk_events(0.05)

            busy_observed = False
            if wait_busy and enter_sent:
                deadline = time.monotonic() + wait_busy
                while time.monotonic() < deadline:
                    pump_gtk_events(0.05)
                    if (terminal.screen_revision > revision_before_enter and
                            terminal_activity_state(terminal) == 'busy'):
                        busy_observed = True
                        break

            pump_gtk_events(0.03)
            after_text = terminal.get_text()
            revision_after = terminal.screen_revision
            state_after = terminal_activity_state(terminal, text=after_text)
            needle = echo_needle(text)
            footer = re.sub(r'\s+', '', '\n'.join(
                after_text.splitlines()[-3:]))
            input_cleared = bool(submit and needle and needle not in footer)
            ok = echo_observed and (not submit or enter_sent)
            code = None
            message = None
            if not echo_observed:
                code = 'INPUT_NOT_ECHOED'
                message = 'text was not observed; Enter was not sent'
            elif wait_busy and not busy_observed:
                ok = False
                code = 'BUSY_NOT_OBSERVED'
                message = 'Enter was sent but no busy transition was observed'

            self.send_sequence += 1
            result = {
                'ok': ok,
                'terminal_uuid': terminal.uuid.urn,
                'bytes_written': len(text.encode('utf-8')),
                'enter_sent': enter_sent,
                'echo_observed': echo_observed,
                'input_cleared': input_cleared,
                'busy_observed': busy_observed,
                'state_transition': '%s->%s' %
                    (state_before, state_after),
                'screen_revision_before': revision_before,
                'screen_revision_after': revision_after,
                'sequence_id': self.send_sequence,
            }
            if code:
                result.update({
                    'code': code,
                    'message': message,
                    'retryable': True,
                })
            metadata = text_metadata(text)
            audit_event(
                'send', terminal_uuid=terminal.uuid.urn,
                label=terminal.agent_label, owner=owner,
                submit=submit, enter_sent=enter_sent,
                screen_revision_before=revision_before,
                screen_revision_after=revision_after, ok=ok,
                **metadata)
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except AgentControlError as ex:
            return json.dumps(ex.as_dict(), ensure_ascii=False, sort_keys=True)

    @dbus.service.method(BUS_NAME)
    def get_terminal_text(self, uuid=None, options=dbus.Dictionary()):
        """Return the text content of the terminal with the given UUID.
        By default the currently visible screen is returned. Options:
          lines:      return only the last N lines of the buffer
          scrollback: 'True' to return the whole scrollback buffer"""
        terminal = self.terminator.find_terminal_by_uuid(uuid)
        if not terminal:
            return "ERROR: Terminal with supplied UUID not found"
        since_revision = options.get('since_revision')
        if since_revision is not None:
            result = terminal.get_text_since(since_revision)
            result.update({
                'ok': True,
                'terminal_uuid': terminal.uuid.urn,
                'screen_mode': 'unknown',
            })
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        return terminal.get_text(
            lines=options.get('lines', 0),
            scrollback=options.get('scrollback') == 'True')

    @dbus.service.method(BUS_NAME)
    def wait_idle(self, options=dbus.Dictionary()):
        """Wait for a stable idle/waiting-input screen or matching output."""
        try:
            terminal = self._find_terminal(
                uuid=options.get('uuid'), label=options.get('label'))
            try:
                stable_ms = max(0, int(options.get('stable_ms', 2000)))
                timeout = max(0.0, float(options.get('timeout', 1800)))
            except (TypeError, ValueError):
                return json_error(
                    'INVALID_TIMEOUT', 'timeout/stable-ms must be numbers')
            contains = options.get('contains')
            deadline = time.monotonic() + timeout
            while True:
                pump_gtk_events(0.05)
                now = time.monotonic()
                text = terminal.get_text()
                state = terminal_activity_state(terminal, text=text)
                stable_for_ms = int(
                    (now - terminal.last_activity_monotonic) * 1000)
                if contains and str(contains) in text:
                    return json_result(
                        terminal_uuid=terminal.uuid.urn,
                        state='matched', stable_for_ms=stable_for_ms,
                        screen_revision=terminal.screen_revision,
                        last_output_at=iso_timestamp(
                            terminal.last_activity_at))
                if (not contains and stable_for_ms >= stable_ms and
                        state in ('idle', 'waiting_input', 'exited')):
                    return json_result(
                        terminal_uuid=terminal.uuid.urn,
                        state=state, stable_for_ms=stable_for_ms,
                        screen_revision=terminal.screen_revision,
                        last_output_at=iso_timestamp(
                            terminal.last_activity_at))
                if now >= deadline:
                    return json_error(
                        'WAIT_TIMEOUT',
                        'terminal did not reach the requested state',
                        retryable=True,
                        terminal_uuid=terminal.uuid.urn,
                        state=state,
                        stable_for_ms=stable_for_ms,
                        screen_revision=terminal.screen_revision)
        except AgentControlError as ex:
            return json.dumps(ex.as_dict(), ensure_ascii=False, sort_keys=True)

    @dbus.service.method(BUS_NAME)
    def acquire_session(self, options=dbus.Dictionary()):
        """Acquire or renew a write lease for a GUI terminal session."""
        try:
            terminal = self._find_terminal(
                uuid=options.get('uuid'), label=options.get('label'))
            owner = str(options.get('owner', '')).strip()
            if not owner:
                return json_error('INVALID_OWNER', 'owner must not be empty')
            try:
                ttl = max(1, min(int(options.get('ttl', 600)), 86400))
            except (TypeError, ValueError):
                return json_error('INVALID_TTL', 'ttl must be an integer')
            self._check_write_lease(terminal, owner)
            expires_wall = time.time() + ttl
            self.terminal_leases[terminal.uuid.urn] = {
                'owner': owner,
                'expires_at_monotonic': time.monotonic() + ttl,
                'expires_at': iso_timestamp(expires_wall),
            }
            audit_event('acquire_session',
                        terminal_uuid=terminal.uuid.urn,
                        owner=owner, ttl=ttl)
            return json_result(
                terminal_uuid=terminal.uuid.urn, owner=owner, ttl=ttl,
                expires_at=iso_timestamp(expires_wall))
        except AgentControlError as ex:
            return json.dumps(ex.as_dict(), ensure_ascii=False, sort_keys=True)

    @dbus.service.method(BUS_NAME)
    def release_session(self, options=dbus.Dictionary()):
        """Release a GUI terminal's write lease."""
        try:
            terminal = self._find_terminal(
                uuid=options.get('uuid'), label=options.get('label'))
            owner = str(options.get('owner', '')).strip()
            lease = self.terminal_leases.get(terminal.uuid.urn)
            if lease and lease['owner'] != owner:
                raise AgentControlError(
                    'SESSION_LEASED', 'lease belongs to another owner',
                    owner=lease['owner'], expires_at=lease['expires_at'])
            self.terminal_leases.pop(terminal.uuid.urn, None)
            audit_event('release_session',
                        terminal_uuid=terminal.uuid.urn, owner=owner)
            return json_result(
                terminal_uuid=terminal.uuid.urn, released=True)
        except AgentControlError as ex:
            return json.dumps(ex.as_dict(), ensure_ascii=False, sort_keys=True)

    @dbus.service.method(BUS_NAME)
    def screenshot_terminal(self, uuid=None, options=dbus.Dictionary()):
        """Save a PNG screenshot of the terminal with the given UUID and
        return the saved path. Options:
          file:   path to save the image to (required)
          window: 'True' to capture the whole window containing the terminal"""
        terminal = self.terminator.find_terminal_by_uuid(uuid)
        if not terminal:
            return "ERROR: Terminal with supplied UUID not found"
        path = options.get('file')
        if not path:
            return "ERROR: No file supplied (use --file)"
        path = os.path.abspath(os.path.expanduser(str(path)))
        if not path.lower().endswith('.png'):
            path += '.png'
        activate = as_bool(options.get('activate'))
        restore = as_bool(options.get('restore'))
        previous_terminal = self.terminator.last_focused_term
        window = terminal.get_toplevel()
        was_iconified = False
        if activate:
            try:
                window_gdk = window.get_window()
                was_iconified = bool(
                    window_gdk and
                    window_gdk.get_state() & Gdk.WindowState.ICONIFIED)
            except (AttributeError, TypeError):
                pass
            window.deiconify()
            terminal.ensure_visible_and_focussed()
            window.present()
            try:
                wait_frame = float(options.get('wait_frame', 0.1))
            except (TypeError, ValueError):
                wait_frame = 0.1
            pump_gtk_events(max(0.0, min(wait_frame, 2.0)))
        widget = terminal
        if options.get('window') == 'True':
            widget = window
        if not widget.get_window():
            if activate and restore:
                if previous_terminal and previous_terminal is not terminal:
                    previous_terminal.ensure_visible_and_focussed()
                if was_iconified:
                    window.iconify()
            return "ERROR: Terminal is not realized on screen"
        try:
            pixbuf = widget_pixbuf(widget)
            # Rule: screenshots keep a 16:9 aspect ratio (letterboxed),
            # unless explicitly disabled with no_ratio
            if options.get('no_ratio') != 'True':
                pixbuf = pad_pixbuf_to_ratio(pixbuf, 16, 9)
            pixbuf.savev(path, 'png', [], [])
        except Exception as ex:
            return "ERROR: Failed to save screenshot: %s" % ex
        finally:
            if activate and restore:
                if previous_terminal and previous_terminal is not terminal:
                    previous_terminal.ensure_visible_and_focussed()
                if was_iconified:
                    window.iconify()
        return path

    @dbus.service.method(BUS_NAME)
    def get_terminal_info(self, uuid=None, options=dbus.Dictionary()):
        """Return a JSON description of the terminal's geometry and state,
        so a remote controller can tell whether content fits on screen"""
        terminal = self.terminator.find_terminal_by_uuid(uuid)
        if not terminal:
            return "ERROR: Terminal with supplied UUID not found"
        vte = terminal.get_vte()
        vadj = vte.get_vadjustment()
        cursor_col, cursor_row = vte.get_cursor_position()
        window = terminal.get_toplevel()
        win_w, win_h = window.get_size()
        alloc = terminal.get_allocation()
        terminal_gdk = terminal.get_window()
        window_gdk = window.get_window()
        buffer_rows = int(vadj.get_upper())
        viewport_row = int(vadj.get_value())
        info = {
            'columns': vte.get_column_count(),
            'rows': vte.get_row_count(),
            'cursor_row': cursor_row,
            'cursor_col': cursor_col,
            'buffer_rows': buffer_rows,
            'viewport_first_row': viewport_row,
            'scrolled_to_bottom':
                viewport_row + vte.get_row_count() >= buffer_rows,
            'char_width': vte.get_char_width(),
            'char_height': vte.get_char_height(),
            'terminal_width': alloc.width,
            'terminal_height': alloc.height,
            'window_width': win_w,
            'window_height': win_h,
            'terminal_realized': terminal_gdk is not None,
            'window_realized': window_gdk is not None,
            'terminal_viewable': bool(terminal_gdk and
                                      terminal_gdk.is_viewable()),
            'window_viewable': bool(window_gdk and window_gdk.is_viewable()),
            'window_maximized': bool(window.is_maximized()),
            'window_title': window.get_title(),
        }
        info.update(self._terminal_identity(terminal))
        info['last_output_at'] = info['last_activity_at']
        try:
            display = Gdk.Display.get_default()
            monitor = display.get_monitor_at_window(window_gdk)
            geo = monitor.get_geometry()
            workarea = monitor.get_workarea()
            window_rect = window_gdk.get_frame_extents()
            terminal_rect = gdk_window_rect(terminal_gdk)
            info.update({
                'monitor_geometry': rect_to_dict(geo),
                'monitor_workarea': rect_to_dict(workarea),
                'window_rect': rect_to_dict(window_rect),
                'terminal_rect': rect_to_dict(terminal_rect),
                'window_fully_on_monitor':
                    rect_fully_inside(window_rect, geo),
                'window_fully_in_workarea':
                    rect_fully_inside(window_rect, workarea),
                'terminal_fully_on_monitor':
                    rect_fully_inside(terminal_rect, geo),
                'terminal_fully_in_workarea':
                    rect_fully_inside(terminal_rect, workarea),
            })
            info['window_fully_on_screen'] = info['window_fully_on_monitor']
            info['screenshot_ready'] = (
                info['terminal_viewable'] and
                info['window_viewable'] and
                info['window_fully_on_monitor'] and
                info['terminal_fully_on_monitor']
            )
        except Exception as ex:
            dbg('could not determine monitor geometry: %s' % ex)
            info['screenshot_ready'] = (
                info['terminal_viewable'] and info['window_viewable']
            )
        return json.dumps(info)

    def _layout_node(self, widget):
        maker = Factory()
        if maker.isinstance(widget, 'Terminal'):
            return {
                'type': 'terminal',
                'uuid': widget.uuid.urn,
                'label': widget.agent_label,
                'session_title': widget.get_session_title(),
            }
        if maker.isinstance(widget, 'Notebook'):
            tabs = []
            for page_number in range(widget.get_n_pages()):
                page = widget.get_nth_page(page_number)
                label = widget.get_tab_label(page)
                tabs.append({
                    'type': 'tab',
                    'uuid': getattr(
                        getattr(label, 'uuid', None), 'urn', None),
                    'title': label.get_label(),
                    'active': page_number == widget.get_current_page(),
                    'child': self._layout_node(page),
                })
            return {
                'type': 'notebook',
                'uuid': widget.uuid.urn,
                'children': tabs,
            }
        if maker.isinstance(widget, 'HPaned') or \
                maker.isinstance(widget, 'VPaned'):
            return {
                'type': ('vsplit' if maker.isinstance(widget, 'HPaned')
                         else 'hsplit'),
                'uuid': widget.uuid.urn,
                'ratio': widget.ratio,
                'children': [
                    self._layout_node(child)
                    for child in widget.get_children()
                    if child is not None
                ],
            }
        return {'type': type(widget).__name__}

    @dbus.service.method(BUS_NAME)
    def get_layout(self, options=dbus.Dictionary()):
        """Return the real Window/Notebook/Paned/Terminal layout tree."""
        window_uuid = options.get('window_uuid')
        windows = self.terminator.windows
        if window_uuid:
            windows = [window for window in windows
                       if window.uuid.urn == window_uuid]
        if not windows:
            return json_error('WINDOW_NOT_FOUND', 'window was not found')
        result = []
        for window in windows:
            result.append({
                'type': 'window',
                'uuid': window.uuid.urn,
                'title': window.get_title(),
                'child': self._layout_node(window.get_child()),
            })
        return json_result(
            windows=result, layout_revision=time.time_ns())

    @dbus.service.method(BUS_NAME)
    def resize_pane(self, uuid=None, options=dbus.Dictionary()):
        """Resize the immediate split so the selected pane gets ratio share."""
        try:
            terminal = self._find_terminal(uuid=uuid)
            try:
                ratio = float(options.get('ratio'))
            except (TypeError, ValueError):
                raise AgentControlError(
                    'INVALID_LAYOUT', 'ratio must be a number from 0.1 to 0.9')
            if ratio < 0.1 or ratio > 0.9:
                raise AgentControlError(
                    'INVALID_LAYOUT', 'ratio must be from 0.1 to 0.9')
            parent = terminal.get_parent()
            maker = Factory()
            if not (maker.isinstance(parent, 'HPaned') or
                    maker.isinstance(parent, 'VPaned')):
                raise AgentControlError(
                    'INVALID_LAYOUT', 'terminal is not directly inside a split')
            parent.ratio = ratio if parent.get_child1() is terminal \
                else 1.0 - ratio
            parent.set_position_by_ratio()
            result = {
                'terminal_uuid': terminal.uuid.urn,
                'ratio': ratio,
                'layout_revision': time.time_ns(),
            }
            audit_event('resize_pane', **result)
            return json_result(**result)
        except AgentControlError as ex:
            return json.dumps(ex.as_dict(), ensure_ascii=False, sort_keys=True)

    @dbus.service.method(BUS_NAME)
    def focus_terminal(self, uuid=None, options=dbus.Dictionary()):
        """Focus a pane, selecting its tab and optionally raising the window."""
        try:
            terminal = self._find_terminal(uuid=uuid)
            terminal.ensure_visible_and_focussed()
            if as_bool(options.get('raise_window')):
                terminal.get_toplevel().present()
            return json_result(terminal_uuid=terminal.uuid.urn, focused=True)
        except AgentControlError as ex:
            return json.dumps(ex.as_dict(), ensure_ascii=False, sort_keys=True)

    @dbus.service.method(BUS_NAME)
    def scrollshot_terminal(self, uuid=None, options=dbus.Dictionary()):
        """Save a long PNG screenshot of a terminal covering its scrollback
        buffer, by scrolling page by page and stitching the grabs. Options:
          file:  path to save the image to (required)
          lines: how many trailing buffer lines to cover (default 2000)"""
        terminal = self.terminator.find_terminal_by_uuid(uuid)
        if not terminal:
            return "ERROR: Terminal with supplied UUID not found"
        path = options.get('file')
        if not path:
            return "ERROR: No file supplied (use --file)"
        path = os.path.abspath(os.path.expanduser(str(path)))
        if not path.lower().endswith('.png'):
            path += '.png'
        vte = terminal.get_vte()
        if not vte.get_window():
            return "ERROR: Terminal is not realized on screen"
        vadj = vte.get_vadjustment()
        orig_value = vadj.get_value()
        visible_rows = int(vte.get_row_count()) or 1
        buffer_rows = max(int(vadj.get_upper()), visible_rows)
        page_rows = int(vadj.get_page_size()) or visible_rows
        try:
            lines = int(options.get('lines', 0))
        except (TypeError, ValueError):
            lines = 0
        if lines <= 0:
            lines = 2000
        first_row = max(0, buffer_rows - lines)
        total_rows = buffer_rows - first_row
        char_h = int(vte.get_char_height()) or 1
        width = int(vte.get_window().get_width())
        height = total_rows * char_h
        if width <= 0 or height <= 0:
            return "ERROR: Terminal has no drawable size"
        if width * height > MAX_SCROLLSHOT_PIXELS:
            return ("ERROR: Requested scrollshot too large: %sx%s pixels "
                    "(use --lines/-n to request fewer rows)" %
                    (width, height))
        try:
            result = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8,
                                          width, height)
            result.fill(0x000000ff)
            row = first_row
            while row < buffer_rows:
                target = max(0, min(row, buffer_rows - page_rows))
                vadj.set_value(target)
                while gtk.events_pending():
                    gtk.main_iteration_do(False)
                grab = widget_pixbuf(vte)
                src_y = max(0, (first_row - target) * char_h)
                dest_y = max(0, (target - first_row) * char_h)
                copy_w = min(grab.get_width(), width)
                copy_h = min(grab.get_height() - src_y, height - dest_y)
                if copy_h > 0:
                    grab.copy_area(0, src_y, copy_w, copy_h,
                                   result, 0, dest_y)
                row = target + page_rows
            result.savev(path, 'png', [], [])
        except Exception as ex:
            return "ERROR: Failed to save scrollshot: %s" % ex
        finally:
            vadj.set_value(orig_value)
            while gtk.events_pending():
                gtk.main_iteration_do(False)
        return path


def escape_decode(text):
    """Decode common backslash escapes (\\n \\r \\t \\e \\xHH) without
    mangling non-ASCII characters"""
    return decode_key_escapes(text)


def with_proxy(func):
    """Decorator function to connect to the session dbus bus"""
    dbg('dbus client call: %s' % func.__name__)
    def _exec(*args, **argd):
        bus = dbus.SessionBus()
        try:
            proxy = bus.get_object(BUS_NAME, BUS_PATH)

        except dbus.DBusException as e:
            sys.exit(
                "Remotinator can't connect to terminator. " +
                "May be terminator is not running.")

        return func(proxy, *args, **argd)
    return _exec

@with_proxy
def new_window_cmdline(session, options):
    """Call the dbus method to open a new window"""
    session.new_window_cmdline(options)

@with_proxy
def new_tab_cmdline(session, options):
    """Call the dbus method to open a new tab in the first window"""
    session.new_tab_cmdline(options)

@with_proxy
def toggle_visibility_cmdline(session,options):
    session.toggle_visibility_cmdline(options)

@with_proxy
def reload_configuration(session):
    """Call the dbus method to reload configuration for all windows"""
    session.reload_configuration()

@with_proxy
def unhide_cmdline(session,options):
    session.unhide_cmdline(options)

@with_proxy
def new_window(session, options):
    """Call the dbus method to open a new window"""
    return session.new_window()

@with_proxy
def new_tab(session, uuid, options):
    """Call the dbus method to open a new tab in the first window"""
    return session.new_tab(uuid)

@with_proxy
def hsplit(session, uuid, options):
    """Call the dbus method to horizontally split a terminal"""
    return session.hsplit(uuid,options)

@with_proxy
def vsplit(session, uuid, options):
    """Call the dbus method to vertically split a terminal"""
    return session.vsplit(uuid,options)

@with_proxy
def split(session, uuid, options):
    """Call the configured atomic split method."""
    return session.split(uuid, options)

@with_proxy
def get_terminals(session, options):
    """Call the dbus method to return a list of all terminals"""
    return '\n'.join(session.get_terminals())

@with_proxy
def list_terminals(session, options):
    """Return aggregated terminal identities as JSON."""
    return session.list_terminals(options)

@with_proxy
def get_focused_terminal(session, options):
    """Call the dbus method to return the currently focused terminal"""
    return session.get_focused_terminal()

@with_proxy
def get_window(session, uuid, options):
    """Call the dbus method to return the toplevel tab for a terminal"""
    return session.get_window(uuid)

@with_proxy
def get_window_title(session, uuid, options):
    """Call the dbus method to return the title of a tab"""
    return session.get_window_title(uuid)

@with_proxy
def get_tab(session, uuid, options):
    """Call the dbus method to return the toplevel tab for a terminal"""
    return session.get_tab(uuid)

@with_proxy
def get_tab_title(session, uuid, options):
    """Call the dbus method to return the title of a tab"""
    return session.get_tab_title(uuid)

@with_proxy
def get_terminal_title(session, uuid, options):
    return session.get_terminal_title(uuid)

@with_proxy
def set_terminal_label(session, uuid, options):
    return session.set_terminal_label(uuid, options)

@with_proxy
def clear_terminal_label(session, uuid, options):
    return session.clear_terminal_label(uuid, options)

@with_proxy
def set_tab_title(session, uuid, options):
    """Call the dbus method to set the title of a tab"""
    return session.set_tab_title(uuid, options)

@with_proxy
def switch_profile(session, uuid, options):
    """Call the dbus method to return the title of a tab"""
    return session.switch_profile(uuid, options)

@with_proxy
def switch_profile_all(session,options):
    """Call the dbus method to return the title of a tab"""
    return session.switch_profile_all(options)

@with_proxy
def bg_img_all(session,options):
    return session.bg_img_all(options)

@with_proxy
def feed_terminal(session, uuid, options):
    """Call the dbus method to feed text to a terminal"""
    text = options.get('text')
    if text is not None:
        options['text'] = escape_decode(text)
    return session.feed_terminal(uuid, options)

@with_proxy
def send(session, options):
    text = options.get('text')
    if text is not None:
        options['text'] = escape_decode(text)
    return session.send(options)

@with_proxy
def get_terminal_text(session, uuid, options):
    """Call the dbus method to get the text content of a terminal"""
    return session.get_terminal_text(uuid, options)

@with_proxy
def screenshot_terminal(session, uuid, options):
    """Call the dbus method to save a screenshot of a terminal"""
    return session.screenshot_terminal(uuid, options)

@with_proxy
def get_terminal_info(session, uuid, options):
    """Call the dbus method to get the geometry/state of a terminal"""
    return session.get_terminal_info(uuid, options)

@with_proxy
def wait_idle(session, options):
    return session.wait_idle(options)

@with_proxy
def acquire_session(session, options):
    return session.acquire_session(options)

@with_proxy
def release_session(session, options):
    return session.release_session(options)

@with_proxy
def get_layout(session, options):
    return session.get_layout(options)

@with_proxy
def resize_pane(session, uuid, options):
    return session.resize_pane(uuid, options)

@with_proxy
def focus_terminal(session, uuid, options):
    return session.focus_terminal(uuid, options)

@with_proxy
def attach_headless(session, uuid, options):
    return session.attach_headless(uuid, options)

@with_proxy
def scrollshot_terminal(session, uuid, options):
    """Call the dbus method to save a long screenshot of a terminal"""
    return session.scrollshot_terminal(uuid, options)

@with_proxy
def bg_img(session,uuid,options):
    return session.bg_img(uuid,options)
