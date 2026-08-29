import io
import json

import pytest

from remem.agents.claude_code import hook
from remem.extract.base import CHILD_ENV_VAR


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """The warn-state path comes from platformdirs, which reads the real
    environment - without this the tests would read and write the
    developer's own cache file and depend on earlier runs."""
    from remem import session_size

    monkeypatch.setattr(
        session_size, "state_path", lambda: tmp_path / "session-size.json"
    )


def _payload(transcript, session_id="sess-1"):
    return json.dumps({"transcript_path": str(transcript),
                       "session_id": session_id})


def _transcript(tmp_path, turns):
    p = tmp_path / "t.jsonl"
    p.write_text('{"type":"user"}\n' * turns)
    return p


def _env(tmp_path, **extra):
    env = {"REMEM_TURN_WARN_AT": "10", "REMEM_TURN_WARN_EVERY": "5",
           "REMEM_CONFIG": str(tmp_path / "none.toml")}
    env.update(extra)
    return env


def test_a_short_session_says_nothing(tmp_path):
    t = _transcript(tmp_path, 3)
    assert hook.session_size(_payload(t), env=_env(tmp_path)) == ""


def test_a_long_session_warns_once_then_stays_quiet(tmp_path):
    t = _transcript(tmp_path, 12)
    env = _env(tmp_path)
    first = hook.session_size(_payload(t), env=env)
    assert "12" in first and "remem-handoff" in first
    assert hook.session_size(_payload(t), env=env) == ""


def test_it_warns_again_a_full_step_later(tmp_path):
    env = _env(tmp_path)
    hook.session_size(_payload(_transcript(tmp_path, 12)), env=env)
    grown = tmp_path / "grown.jsonl"
    grown.write_text('{"type":"user"}\n' * 17)
    assert hook.session_size(_payload(grown), env=env) != ""


def test_malformed_stdin_is_silent(tmp_path):
    assert hook.session_size("{not json", env=_env(tmp_path)) == ""


def test_a_missing_transcript_path_is_silent(tmp_path):
    assert hook.session_size(json.dumps({"session_id": "x"}),
                             env=_env(tmp_path)) == ""


def test_a_capture_child_is_never_warned(tmp_path):
    """The extraction child is a session remem started; telling it to hand
    off would be advice to nobody."""
    t = _transcript(tmp_path, 99)
    env = _env(tmp_path, **{CHILD_ENV_VAR: "1"})
    assert hook.session_size(_payload(t), env=env) == ""


def test_main_always_exits_zero(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
    assert hook.main_session_size() == 0
