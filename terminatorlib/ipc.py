# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""ipc.py - DBus server and API calls"""

import os
import re
import sys
import json
import hashlib
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


class DBusService(Borg, dbus.service.Object):
    """DBus Server class. This is implemented as a Borg"""
    bus_name = None
    bus_path = None
    terminator = None

    def __init__(self):
        """Class initialiser"""
        Borg.__init__(self, self.__class__.__name__)
        self.prepare_attributes()
        dbus.service.Object.__init__(self, self.bus_name, BUS_PATH)

    def prepare_attributes(self):
        """Ensure we are populated"""
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
        maker = Factory()
        terminal = self.terminator.find_terminal_by_uuid(uuid)
        window = terminal.get_toplevel()
        root_widget = window.get_children()[0]
        if maker.isinstance(root_widget, 'Notebook'):
            #return root_widget.uuid.urn
            for tab_child in root_widget.get_children():
                terms = [tab_child]
                if not maker.isinstance(terms[0], "Terminal"):
                    terms = enumerate_descendants(tab_child)[1]
                if terminal in terms:
                    # FIXME: There are no uuid's assigned to the the notebook, or the actual tabs!
                    # This would fail: return root_widget.uuid.urn
                    return ""

    @dbus.service.method(BUS_NAME)
    def get_tab_title(self, uuid=None):
        """Return the title of a parent tab of a given terminal"""
        maker = Factory()
        terminal = self.terminator.find_terminal_by_uuid(uuid)
        window = terminal.get_toplevel()
        root_widget = window.get_children()[0]
        if maker.isinstance(root_widget, "Notebook"):
            for tab_child in root_widget.get_children():
                terms = [tab_child]
                if not maker.isinstance(terms[0], "Terminal"):
                    terms = enumerate_descendants(tab_child)[1]
                if terminal in terms:
                    return root_widget.get_tab_label(tab_child).get_label()

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
        terminal.feed(str(text))
        return "OK"

    @dbus.service.method(BUS_NAME)
    def get_terminal_text(self, uuid=None, options=dbus.Dictionary()):
        """Return the text content of the terminal with the given UUID.
        By default the currently visible screen is returned. Options:
          lines:      return only the last N lines of the buffer
          scrollback: 'True' to return the whole scrollback buffer"""
        terminal = self.terminator.find_terminal_by_uuid(uuid)
        if not terminal:
            return "ERROR: Terminal with supplied UUID not found"
        vte = terminal.get_vte()
        vadj = vte.get_vadjustment()
        # Vte's get_text_range[_format] uses ABSOLUTE row indices, the same
        # coordinate space as get_cursor_position().  The scroll adjustment is
        # compacted to the retained scrollback (its upper is the row *count*,
        # capped at scrollback_lines), so once a terminal emits more lines than
        # its scrollback limit the two coordinate systems diverge.  Anchor on
        # the cursor row instead, otherwise we read already-evicted rows and get
        # back only blank lines.
        _col, last_row = vte.get_cursor_position()
        retained = max(1, int(vadj.get_upper() - vadj.get_lower()))
        if options.get('scrollback') == 'True':
            start_row = max(0, last_row - retained + 1)
        else:
            try:
                lines = int(options.get('lines', 0))
            except (TypeError, ValueError):
                lines = 0
            if lines <= 0:
                lines = vte.get_row_count()
            start_row = max(0, last_row - lines + 1)
        end_col = vte.get_column_count() - 1
        if Vte.get_minor_version() < 72:
            text = vte.get_text_range(start_row, 0, last_row, end_col,
                                      lambda *a: True)[0]
        else:
            text = vte.get_text_range_format(Vte.Format.TEXT, start_row, 0,
                                             last_row, end_col)[0]
        return text or ''

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
        widget = terminal
        if options.get('window') == 'True':
            widget = terminal.get_toplevel()
        if not widget.get_window():
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
    escapes = {'\\n': '\n', '\\r': '\r', '\\t': '\t',
               '\\e': '\x1b', '\\\\': '\\'}
    def _sub(match):
        seq = match.group(0)
        if seq.startswith('\\x'):
            return chr(int(seq[2:], 16))
        return escapes[seq]
    return re.sub(r'\\x[0-9a-fA-F]{2}|\\[nrte\\]', _sub, text)


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
    print(session.new_window())

@with_proxy
def new_tab(session, uuid, options):
    """Call the dbus method to open a new tab in the first window"""
    print(session.new_tab(uuid))

@with_proxy
def hsplit(session, uuid, options):
    """Call the dbus method to horizontally split a terminal"""
    print(session.hsplit(uuid,options))

@with_proxy
def vsplit(session, uuid, options):
    """Call the dbus method to vertically split a terminal"""
    print(session.vsplit(uuid,options))

@with_proxy
def get_terminals(session, options):
    """Call the dbus method to return a list of all terminals"""
    print('\n'.join(session.get_terminals()))

@with_proxy
def get_focused_terminal(session, options):
    """Call the dbus method to return the currently focused terminal"""
    return session.get_focused_terminal()

@with_proxy
def get_window(session, uuid, options):
    """Call the dbus method to return the toplevel tab for a terminal"""
    print(session.get_window(uuid))

@with_proxy
def get_window_title(session, uuid, options):
    """Call the dbus method to return the title of a tab"""
    print(session.get_window_title(uuid))

@with_proxy
def get_tab(session, uuid, options):
    """Call the dbus method to return the toplevel tab for a terminal"""
    print(session.get_tab(uuid))

@with_proxy
def get_tab_title(session, uuid, options):
    """Call the dbus method to return the title of a tab"""
    print(session.get_tab_title(uuid))

@with_proxy
def set_tab_title(session, uuid, options):
    """Call the dbus method to set the title of a tab"""
    session.set_tab_title(uuid, options)

@with_proxy
def switch_profile(session, uuid, options):
    """Call the dbus method to return the title of a tab"""
    session.switch_profile(uuid, options)

@with_proxy
def switch_profile_all(session,options):
    """Call the dbus method to return the title of a tab"""
    session.switch_profile_all(options)

@with_proxy
def bg_img_all(session,options):
    session.bg_img_all(options)

@with_proxy
def feed_terminal(session, uuid, options):
    """Call the dbus method to feed text to a terminal"""
    text = options.get('text')
    if text is not None:
        options['text'] = escape_decode(text)
    return session.feed_terminal(uuid, options)

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
def scrollshot_terminal(session, uuid, options):
    """Call the dbus method to save a long screenshot of a terminal"""
    return session.scrollshot_terminal(uuid, options)

@with_proxy
def bg_img(session,uuid,options):
    session.bg_img(uuid,options)
