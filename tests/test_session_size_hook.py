import io
import json
from pathlib import Path

import pytest

from saddlebag.agents.claude_code import hook
from saddlebag.extract.base import CHILD_ENV_VAR


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The warn-state path comes from platformdirs, which reads the real
    environment - without this the tests would read and write the
    developer's own cache file and depend on earlier runs."""
    from saddlebag import session_size

    monkeypatch.setattr(
        session_size, "state_path", lambda: tmp_path / "session-size.json"
    )


def _payload(transcript: Path, session_id: str = "sess-1") -> str:
    return json.dumps({"transcript_path": str(transcript), "session_id": session_id})


def _transcript(tmp_path: Path, turns: int) -> Path:
    p = tmp_path / "t.jsonl"
    p.write_text('{"type":"user"}\n' * turns)
    return p


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "BAG_TURN_WARN_AT": "10",
        "BAG_TURN_WARN_EVERY": "5",
        "BAG_CONFIG": str(tmp_path / "none.toml"),
    }
    env.update(extra)
    return env


def test_a_short_session_says_nothing(tmp_path: Path) -> None:
    t = _transcript(tmp_path, 3)
    assert hook.session_size(_payload(t), env=_env(tmp_path)) == ""


def test_a_long_session_warns_once_then_stays_quiet(tmp_path: Path) -> None:
    t = _transcript(tmp_path, 12)
    env = _env(tmp_path)
    first = hook.session_size(_payload(t), env=env)
    assert "12" in first and "bag-handoff" in first
    assert hook.session_size(_payload(t), env=env) == ""


def test_it_warns_again_a_full_step_later(tmp_path: Path) -> None:
    env = _env(tmp_path)
    hook.session_size(_payload(_transcript(tmp_path, 12)), env=env)
    grown = tmp_path / "grown.jsonl"
    grown.write_text('{"type":"user"}\n' * 17)
    assert hook.session_size(_payload(grown), env=env) != ""


def test_malformed_stdin_is_silent(tmp_path: Path) -> None:
    assert hook.session_size("{not json", env=_env(tmp_path)) == ""


def test_a_missing_transcript_path_is_silent(tmp_path: Path) -> None:
    assert hook.session_size(json.dumps({"session_id": "x"}), env=_env(tmp_path)) == ""


def test_a_capture_child_is_never_warned(tmp_path: Path) -> None:
    """The extraction child is a session saddlebag started; telling it to hand
    off would be advice to nobody."""
    t = _transcript(tmp_path, 99)
    env = _env(tmp_path, **{CHILD_ENV_VAR: "1"})
    assert hook.session_size(_payload(t), env=env) == ""


def test_main_always_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
    assert hook.main_session_size() == 0
