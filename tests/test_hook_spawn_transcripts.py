"""The session-start hook's detached `bag transcripts refresh`.

The fourth sibling of `spawn_process`, `spawn_ingest` and `spawn_memory`,
making the same promises: never block the session, never print into it,
never recurse into the extractor's own child. The refresh is bounded before
it reads, so a session start never pays for a backfill that can be 179MB -
that cost belongs to `bag transcripts import`, typed and loud, never to a
spawn.
"""

import subprocess
from collections.abc import Mapping
from typing import Any, NoReturn

import pytest

from saddlebag import hookio
from saddlebag.extract.base import CHILD_ENV_VAR


def test_spawn_transcripts_launches_the_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_popen(cmd: list[str], **kwargs: Any) -> object:
        seen["cmd"] = cmd
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert hookio.spawn_transcripts({"PATH": "/usr/bin"}) is True
    assert seen["cmd"][:3] == ["bag", "transcripts", "refresh"]


def test_spawn_transcripts_does_not_block_on_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_popen(cmd: list[str], **kwargs: Any) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    hookio.spawn_transcripts({})
    assert seen.get("stdout") == subprocess.DEVNULL
    assert seen.get("stderr") == subprocess.DEVNULL
    assert seen.get("start_new_session") is True


def test_spawn_transcripts_refuses_to_recurse(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fourth CHILD_ENV_VAR check.

    The extractor spawns `claude -p`, whose own hooks would otherwise spawn
    a transcript import, which would read the transcript that child is
    writing, without bound.
    """
    called = []

    def fake_popen(*a: object, **k: object) -> None:
        called.append(1)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert hookio.spawn_transcripts({CHILD_ENV_VAR: "1"}) is False
    assert called == []


def test_spawn_transcripts_returns_false_when_saddlebag_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: object, **k: object) -> NoReturn:
        raise FileNotFoundError("bag")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert hookio.spawn_transcripts({}) is False


# --- both trigger points, because there are two and only two -----------
# Claude Code reaches this from SessionStart; opencode and Cursor reach it
# from `bag hook context`. Spawning from one and not the other is exactly
# the bug that left Cursor-only installs recording forever and extracting
# never.
def test_session_start_spawns_a_transcript_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saddlebag.agents.claude_code import hook

    calls: list[str] = []

    def record_spawn(env: Mapping[str, str]) -> bool:
        calls.append("t")
        return True

    def already_spawned(env: Mapping[str, str]) -> bool:
        return True

    monkeypatch.setattr(
        "saddlebag.agents.claude_code.hook.spawn_transcripts", record_spawn
    )
    monkeypatch.setattr(
        "saddlebag.agents.claude_code.hook.spawn_process", already_spawned
    )
    monkeypatch.setattr(
        "saddlebag.agents.claude_code.hook.spawn_ingest", already_spawned
    )
    monkeypatch.setattr(
        "saddlebag.agents.claude_code.hook.spawn_memory", already_spawned
    )
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))

    assert hook.main() == 0
    assert calls == ["t"]


def test_session_start_still_prints_nothing_when_the_import_cannot_spawn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail-soft is not weakened by adding a fourth spawn."""
    from saddlebag.agents.claude_code import hook

    def boom(*a: object, **k: object) -> NoReturn:
        raise OSError("no processes")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""
