# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""Helpers for the agent-control API and tmux-backed headless sessions.

The GUI-facing D-Bus API stays in :mod:`terminatorlib.ipc`.  This module is
GTK-free so headless sessions remain alive when Terminator exits.  tmux is an
explicit transitional PTY backend, as recommended by AGENT_CONTROL.md; no
hidden GTK windows or VTE widgets are created.
"""

import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid as uuid_module


HEADLESS_PREFIX = 'terminator-agent-'
BUSY_MARKERS = (
    'esc to interrupt',
    'esc to cancel',
    'ctrl+c to interrupt',
    'working (',
    'thinking',
)
INTERACTIVE_PROCESS_MARKERS = (
    'claude', 'codex', 'gemini', 'glm', 'hermes',
    'mimocode', 'nvim', 'vim', 'tmux',
)


class AgentControlError(Exception):
    """An expected, structured agent-control failure."""

    def __init__(self, code, message, retryable=False, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details

    def as_dict(self):
        result = {
            'ok': False,
            'code': self.code,
            'message': self.message,
            'retryable': self.retryable,
        }
        result.update(self.details)
        return result


def iso_timestamp(timestamp=None):
    """Return an ISO-8601 local timestamp with an explicit UTC offset."""
    if timestamp is None:
        timestamp = time.time()
    return datetime.datetime.fromtimestamp(
        timestamp, datetime.timezone.utc).astimezone().isoformat()


def json_result(ok=True, **fields):
    result = {'ok': bool(ok)}
    result.update(fields)
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def json_error(code, message, retryable=False, **fields):
    return json.dumps(AgentControlError(
        code, message, retryable, **fields).as_dict(),
        ensure_ascii=False, sort_keys=True)


def as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def contains_busy_marker(text):
    """Look only at the live footer, not stale markers in scrollback."""
    footer = '\n'.join((text or '').lower().splitlines()[-8:])
    return any(marker in footer for marker in BUSY_MARKERS)


def process_waits_for_input(name, argv=None):
    """Recognise interactive programs whose stable screen means a prompt."""
    command = ' '.join([name or ''] + list(argv or [])).lower()
    return any(marker in command for marker in INTERACTIVE_PROCESS_MARKERS)


def echo_needle(text, limit=120):
    """Choose a wrapping-insensitive fragment for input echo verification."""
    fragments = [
        re.sub(r'\s+', '', line)[-limit:]
        for line in (text or '').splitlines()
        if re.sub(r'\s+', '', line)
    ]
    if not fragments:
        return ''
    return max(fragments, key=len)


def decode_key_escapes(text):
    """Decode remotinator's portable key escape syntax."""
    escapes = {'\\n': '\n', '\\r': '\r', '\\t': '\t',
               '\\e': '\x1b', '\\\\': '\\'}

    def replace(match):
        sequence = match.group(0)
        if sequence.startswith('\\x'):
            return chr(int(sequence[2:], 16))
        return escapes[sequence]

    return re.sub(r'\\x[0-9a-fA-F]{2}|\\[nrte\\]', replace, text)


def audit_event(operation, **metadata):
    """Append metadata-only JSONL; input text is represented by length/hash."""
    state_home = os.environ.get(
        'XDG_STATE_HOME', os.path.expanduser('~/.local/state'))
    directory = os.path.join(state_home, 'terminator')
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        record = {
            'timestamp': iso_timestamp(),
            'operation': operation,
        }
        record.update(metadata)
        path = os.path.join(directory, 'agent-control.jsonl')
        descriptor = os.open(
            path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        os.chmod(path, 0o600)
        with os.fdopen(descriptor, 'a', encoding='utf-8') as output:
            output.write(json.dumps(
                record, ensure_ascii=False, sort_keys=True) + '\n')
    except OSError:
        # Auditing must not turn a successful terminal operation into a
        # failure.  Callers still receive operation metadata in the response.
        pass


def text_metadata(text):
    encoded = (text or '').encode('utf-8')
    return {
        'text_bytes': len(encoded),
        'text_sha256': hashlib.sha256(encoded).hexdigest(),
    }


def _tmux(*args, input_bytes=None, check=True):
    if not shutil.which('tmux'):
        raise AgentControlError(
            'HEADLESS_BACKEND_UNAVAILABLE',
            'tmux is required for headless sessions but was not found')
    proc = subprocess.run(
        ['tmux'] + [str(arg) for arg in args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and proc.returncode:
        raise AgentControlError(
            'HEADLESS_BACKEND_ERROR',
            proc.stderr.decode('utf-8', 'replace').strip() or
            'tmux command failed',
            retryable=True,
        )
    return proc


def _option(name, key, default=''):
    proc = _tmux('show-options', '-v', '-t', name, key, check=False)
    if proc.returncode:
        return default
    return proc.stdout.decode('utf-8', 'replace').rstrip('\n')


def _display(name, fmt):
    return _tmux(
        'display-message', '-p', '-t', name, fmt
    ).stdout.decode('utf-8', 'replace').strip()


def _session_names():
    proc = _tmux('list-sessions', '-F', '#{session_name}', check=False)
    if proc.returncode:
        return []
    return [name for name in
            proc.stdout.decode('utf-8', 'replace').splitlines()
            if name.startswith(HEADLESS_PREFIX)]


def _session_dict(name):
    session_id = _option(name, '@terminator_session_id')
    if not session_id:
        session_id = 'session:' + name[len(HEADLESS_PREFIX):]
    pane_dead = _display(name, '#{pane_dead}') == '1'
    foreground_process = _display(name, '#{pane_current_command}')
    try:
        activity_timestamp = int(_display(name, '#{session_activity}'))
    except (TypeError, ValueError):
        activity_timestamp = int(time.time())
    recent = time.time() - activity_timestamp < 2.0
    shell_names = {'bash', 'dash', 'fish', 'ksh', 'sh', 'tcsh', 'zsh'}
    if pane_dead:
        activity_state = 'exited'
    elif recent:
        activity_state = 'busy'
    elif foreground_process in shell_names:
        activity_state = 'idle'
    elif process_waits_for_input(foreground_process):
        activity_state = 'waiting_input'
    else:
        activity_state = 'busy'
    return {
        'session_id': session_id,
        'backend_id': name,
        'agent_label': _option(name, '@terminator_agent_label') or None,
        'cwd': (_display(name, '#{pane_current_path}') or
                _option(name, '@terminator_cwd')),
        'pid': int(_display(name, '#{pane_pid}') or 0),
        'foreground_process': foreground_process,
        'headless': _display(name, '#{session_attached}') == '0',
        'attached_views': int(_display(name, '#{session_attached}') or 0),
        'activity_state': activity_state,
        'last_activity_at': iso_timestamp(activity_timestamp),
        'screen_revision': activity_timestamp,
    }


def list_headless_sessions():
    sessions = [_session_dict(name) for name in _session_names()]
    counts = {}
    for session in sessions:
        label = session.get('agent_label')
        if label:
            counts[label] = counts.get(label, 0) + 1
    warnings = [
        {'code': 'DUPLICATE_LABEL', 'label': label, 'count': count}
        for label, count in sorted(counts.items()) if count > 1
    ]
    return {'ok': True, 'sessions': sessions, 'warnings': warnings}


def _auto_label(execute):
    command = (execute or os.environ.get('SHELL') or 'Headless').lower()
    stem = 'Headless'
    for candidate in ('Claude', 'Codex', 'GLM', 'Hermes'):
        if candidate.lower() in command:
            stem = candidate
            break
    existing = {
        item.get('agent_label')
        for item in list_headless_sessions()['sessions']
    }
    suffix = 1
    while '%s-%d' % (stem, suffix) in existing:
        suffix += 1
    return '%s-%d' % (stem, suffix)


def create_headless_session(label=None, cwd=None, execute=None):
    sessions = list_headless_sessions()['sessions']
    if len(sessions) >= 64:
        raise AgentControlError(
            'SESSION_LIMIT_REACHED', 'headless session limit (64) reached')
    cwd = os.path.abspath(os.path.expanduser(cwd or os.getcwd()))
    if not os.path.isdir(cwd):
        raise AgentControlError('INVALID_CWD', 'directory does not exist: %s' % cwd)
    session_uuid = str(uuid_module.uuid4())
    session_id = 'session:' + session_uuid
    name = HEADLESS_PREFIX + session_uuid
    label = (label or '').strip() or _auto_label(execute)
    command = execute or os.environ.get('SHELL') or '/bin/sh'
    # Start a durable shell first, set remain-on-exit, then atomically replace
    # the pane command.  This avoids losing the tmux session when a requested
    # command exits before its metadata options can be written.
    _tmux('new-session', '-d', '-s', name, '-c', cwd,
          os.environ.get('SHELL') or '/bin/sh')
    try:
        _tmux('set-option', '-q', '-t', name, 'remain-on-exit', 'on')
        _tmux('set-option', '-q', '-t', name,
              '@terminator_session_id', session_id)
        _tmux('set-option', '-q', '-t', name,
              '@terminator_agent_label', label)
        _tmux('set-option', '-q', '-t', name,
              '@terminator_cwd', cwd)
        _tmux('respawn-pane', '-k', '-t', name, '-c', cwd, command)
    except Exception:
        _tmux('kill-session', '-t', name, check=False)
        raise
    result = _session_dict(name)
    result['ok'] = True
    audit_event('create_session', session_id=session_id,
                label=label, cwd=cwd)
    return result


def resolve_headless_session(session_id=None, label=None):
    matches = []
    for name in _session_names():
        item = _session_dict(name)
        if session_id and (item['session_id'] == session_id or
                           item['backend_id'] == session_id):
            matches.append(item)
        elif label and item.get('agent_label') == label:
            matches.append(item)
    if not matches:
        raise AgentControlError(
            'SESSION_NOT_FOUND', 'headless session was not found')
    if len(matches) > 1:
        raise AgentControlError(
            'AMBIGUOUS_LABEL', 'label matches multiple headless sessions',
            matches=[item['session_id'] for item in matches])
    return matches[0]


def capture_headless_text(session_id=None, label=None, lines=200):
    session = resolve_headless_session(session_id, label)
    try:
        lines = max(1, min(int(lines), 100000))
    except (TypeError, ValueError):
        lines = 200
    output = _tmux(
        'capture-pane', '-p', '-J', '-t', session['backend_id'],
        '-S', '-%d' % lines,
    ).stdout.decode('utf-8', 'replace')
    return {'ok': True, 'session_id': session['session_id'], 'text': output}


def feed_headless_session(session_id=None, label=None, text='',
                          submit=False, verify_echo=False):
    session = resolve_headless_session(session_id, label)
    before = capture_headless_text(session['session_id'])['text']
    buffer_name = 'terminator-agent-' + uuid_module.uuid4().hex
    encoded = (text or '').encode('utf-8')
    _tmux('load-buffer', '-b', buffer_name, '-', input_bytes=encoded)
    _tmux('paste-buffer', '-d', '-b', buffer_name,
          '-t', session['backend_id'])
    echo_observed = not verify_echo
    if verify_echo:
        needle = echo_needle(text, 80)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            current = capture_headless_text(session['session_id'])['text']
            footer = '\n'.join(current.splitlines()[-32:])
            if (not needle or
                    (current != before and
                     needle in re.sub(r'\s+', '', footer))):
                echo_observed = True
                break
            time.sleep(0.05)
    enter_sent = False
    if submit and echo_observed:
        _tmux('send-keys', '-t', session['backend_id'], 'Enter')
        enter_sent = True
    ok = echo_observed and (not submit or enter_sent)
    result = {
        'ok': ok,
        'session_id': session['session_id'],
        'bytes_written': len(encoded),
        'enter_sent': enter_sent,
        'echo_observed': echo_observed,
        'sequence_id': time.time_ns(),
    }
    if not ok:
        result.update({
            'code': 'INPUT_NOT_ECHOED',
            'message': 'text was not observed; Enter was not sent',
            'retryable': True,
        })
    metadata = text_metadata(text)
    audit_event('feed_session', session_id=session['session_id'],
                label=session.get('agent_label'), submit=submit,
                enter_sent=enter_sent, **metadata)
    # Keep this local for debuggability without exposing the input itself.
    result['screen_changed'] = before != capture_headless_text(
        session['session_id'])['text']
    return result


def wait_headless_session(session_id=None, label=None, stable_ms=2000,
                          timeout=1800, contains=None):
    session = resolve_headless_session(session_id, label)
    stable_ms = max(0, int(stable_ms))
    timeout = max(0.0, float(timeout))
    deadline = time.monotonic() + timeout
    previous = None
    stable_since = time.monotonic()
    while True:
        text = capture_headless_text(session['session_id'])['text']
        now = time.monotonic()
        if text != previous:
            previous = text
            stable_since = now
        stable_for_ms = int((now - stable_since) * 1000)
        if contains and contains in text:
            state = 'matched'
            break
        if (not contains and stable_for_ms >= stable_ms and
                not contains_busy_marker(text)):
            state = 'idle'
            break
        if now >= deadline:
            return {
                'ok': False,
                'code': 'WAIT_TIMEOUT',
                'message': 'session did not reach the requested state',
                'retryable': True,
                'session_id': session['session_id'],
                'state': 'busy',
                'stable_for_ms': stable_for_ms,
            }
        time.sleep(0.2)
    return {
        'ok': True,
        'session_id': session['session_id'],
        'state': state,
        'stable_for_ms': stable_for_ms,
    }


def detach_headless_session(session_id=None, label=None):
    session = resolve_headless_session(session_id, label)
    _tmux('detach-client', '-s', session['backend_id'], check=False)
    audit_event('detach_session', session_id=session['session_id'],
                label=session.get('agent_label'))
    return {'ok': True, 'session_id': session['session_id'], 'headless': True}


def terminate_headless_session(session_id=None, label=None,
                               signal_name='TERM'):
    session = resolve_headless_session(session_id, label)
    signal_name = str(signal_name or 'TERM').upper()
    if signal_name.startswith('SIG'):
        signal_name = signal_name[3:]
    signal_value = getattr(signal, 'SIG' + signal_name, None)
    if signal_value is None:
        raise AgentControlError('INVALID_SIGNAL', 'unknown signal: %s' % signal_name)
    pid = session.get('pid')
    if pid and session.get('activity_state') != 'exited':
        try:
            os.killpg(os.getpgid(pid), signal_value)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if _tmux('has-session', '-t', session['backend_id'],
                 check=False).returncode:
            break
        if _display(session['backend_id'], '#{pane_dead}') == '1':
            break
        time.sleep(0.05)
    _tmux('kill-session', '-t', session['backend_id'], check=False)
    running = _tmux('has-session', '-t', session['backend_id'],
                    check=False).returncode == 0
    audit_event('terminate_session', session_id=session['session_id'],
                label=session.get('agent_label'), signal=signal_name,
                process_still_running=running)
    if running:
        raise AgentControlError(
            'PROCESS_STILL_RUNNING', 'headless session is still running',
            retryable=True)
    return {'ok': True, 'session_id': session['session_id'],
            'signal': signal_name, 'terminated': True}
