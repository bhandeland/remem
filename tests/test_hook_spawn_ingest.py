"""The session-start hook's detached `remem reingest run`.

The sibling of `spawn_process`, and it makes the same three promises: never
block the session, never print into it, never recurse into the extractor's
own child. Re-ingest was manual for the life of the feature, and manual
meant it drifted - two days of doc writing left 32 chunks unindexed.
"""

import subprocess
from collections.abc import Mapping
from typing import Any, NoReturn

import pytest

from remem import hookio
from remem.extract.base import CHILD_ENV_VAR


def test_spawn_ingest_launches_the_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def fake_popen(cmd: list[str], **kwargs: Any) -> object:
        seen["cmd"] = cmd
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert hookio.spawn_ingest({"PATH": "/usr/bin"}) is True
    assert seen["cmd"][:3] == ["remem", "reingest", "run"]


def test_spawn_ingest_does_not_block_on_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = {}

    def fake_popen(cmd: list[str], **kwargs: Any) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    hookio.spawn_ingest({})
    assert seen.get("stdout") == subprocess.DEVNULL
    assert seen.get("stderr") == subprocess.DEVNULL
    assert seen.get("start_new_session") is True


def test_spawn_ingest_is_skipped_inside_an_extraction_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extractor's `claude -p` runs remem's hooks. It must not re-ingest.

    Same guard, same reason, as `spawn_process`: an extraction child that
    spawns work which the next extraction reads has no bound.
    """
    called = []

    def fake_popen(*a: object, **k: object) -> None:
        called.append(1)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert hookio.spawn_ingest({CHILD_ENV_VAR: "1"}) is False
    assert called == []


def test_spawn_ingest_returns_false_when_remem_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: object, **k: object) -> NoReturn:
        raise FileNotFoundError("remem")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert hookio.spawn_ingest({}) is False


# --- both trigger points, because there are two and only two -----------
# Claude Code reaches this from SessionStart; opencode and Cursor reach it
# from `remem hook context`. Spawning from one and not the other is exactly
# the bug that left Cursor-only installs recording forever and extracting
# never.
def test_session_start_spawns_the_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    from remem.agents.claude_code import hook

    spawned: list[Mapping[str, str]] = []

    def record_spawn(env: Mapping[str, str]) -> bool:
        spawned.append(env)
        return True

    def already_spawned(env: Mapping[str, str]) -> bool:
        return True

    monkeypatch.setattr("remem.agents.claude_code.hook.spawn_ingest", record_spawn)
    monkeypatch.setattr("remem.agents.claude_code.hook.spawn_process", already_spawned)
    monkeypatch.setattr("remem.agents.claude_code.hook.spawn_memory", already_spawned)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))

    assert hook.main() == 0
    assert len(spawned) == 1


def test_session_start_still_prints_nothing_when_the_refresh_cannot_spawn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail-soft is not weakened by adding a second spawn."""
    from remem.agents.claude_code import hook

    def boom(*a: object, **k: object) -> NoReturn:
        raise OSError("no processes")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""
