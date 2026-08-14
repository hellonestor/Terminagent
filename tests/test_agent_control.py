import json
import shutil
import uuid

import pytest

from terminatorlib.agent_control import (
    capture_headless_text,
    contains_busy_marker,
    create_headless_session,
    decode_key_escapes,
    echo_needle,
    feed_headless_session,
    json_error,
    list_headless_sessions,
    terminate_headless_session,
    wait_headless_session,
)


def test_key_escape_decoder_preserves_unicode():
    assert decode_key_escapes('中文\\r\\x03') == '中文\r\x03'


def test_echo_needle_ignores_wrapping_and_uses_meaningful_line():
    text = 'short\n这是较长的一行任务内容 with spaces\nend'
    assert echo_needle(text) == '这是较长的一行任务内容withspaces'


def test_busy_marker_only_looks_at_live_footer():
    stale = 'esc to interrupt\n' + '\n'.join('done' for _ in range(10))
    assert not contains_busy_marker(stale)
    assert contains_busy_marker('result\nWorking (12s • esc to interrupt)')


def test_json_errors_are_structured():
    result = json.loads(json_error(
        'TERMINAL_NOT_FOUND', 'missing', retryable=False))
    assert result == {
        'ok': False,
        'code': 'TERMINAL_NOT_FOUND',
        'message': 'missing',
        'retryable': False,
    }


@pytest.mark.skipif(shutil.which('tmux') is None, reason='tmux is unavailable')
def test_headless_session_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    label = 'pytest-%s' % uuid.uuid4()
    session_id = None
    try:
        created = create_headless_session(
            label=label, cwd=str(tmp_path),
            execute='bash --noprofile --norc')
        session_id = created['session_id']
        assert created['headless'] is True
        assert created['agent_label'] == label

        sent = feed_headless_session(
            session_id=session_id, text='echo HEADLESS_PYTEST_OK',
            submit=True, verify_echo=True)
        assert sent['ok'] is True
        assert sent['enter_sent'] is True
        assert sent['echo_observed'] is True

        waited = wait_headless_session(
            session_id=session_id, contains='HEADLESS_PYTEST_OK', timeout=5)
        assert waited['ok'] is True
        assert 'HEADLESS_PYTEST_OK' in capture_headless_text(
            session_id=session_id)['text']
        assert any(
            item['session_id'] == session_id
            for item in list_headless_sessions()['sessions'])
    finally:
        if session_id:
            terminate_headless_session(session_id=session_id)

    audit_path = tmp_path / 'state' / 'terminator' / 'agent-control.jsonl'
    assert audit_path.exists()
    assert audit_path.stat().st_mode & 0o777 == 0o600
