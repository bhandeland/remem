"""The session-start hook's detached `bag memory refresh`.

The third sibling of `spawn_process` and `spawn_ingest`, making the same
three promises: never block the session, never print into it, never recurse
into the extractor's own child. Sync was manual for the life of the
feature, and `memory_runs` was built one branch ahead of this caller - the
one with no terminal to report to.
"""

import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from saddlebag import hookio
from saddlebag.extract.base import CHILD_ENV_VAR


def test_spawn_memory_launches_the_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_popen(cmd: Sequence[str], **kwargs: object) -> object:
        seen["cmd"] = cmd
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert hookio.spawn_memory({"PATH": "/usr/bin"}) is True
    assert seen["cmd"][:3] == ["bag", "memory", "refresh"]


def test_spawn_memory_does_not_block_on_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_popen(cmd: Sequence[str], **kwargs: object) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    hookio.spawn_memory({})
    assert seen.get("stdout") == subprocess.DEVNULL
    assert seen.get("stderr") == subprocess.DEVNULL
    assert seen.get("start_new_session") is True


def test_spawn_memory_is_skipped_inside_an_extraction_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extractor's `claude -p` runs saddlebag's hooks. It must not sync.

    Same guard, same reason, as its two siblings: an extraction child that
    spawns work which the next extraction reads has no bound. Sync is the
    worst of the three to leave unguarded, because it writes files into a
    directory Claude Code is itself writing.
    """
    called = []

    def fake_popen(*a: object, **k: object) -> None:
        called.append(1)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert hookio.spawn_memory({CHILD_ENV_VAR: "1"}) is False
    assert called == []


def test_spawn_memory_returns_false_when_saddlebag_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise FileNotFoundError("bag")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert hookio.spawn_memory({}) is False


# --- both trigger points, because there are two and only two -----------
def test_session_start_spawns_the_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    from saddlebag.agents.claude_code import hook

    spawned: list[Mapping[str, str]] = []

    def record_spawn(env: Mapping[str, str]) -> bool:
        spawned.append(env)
        return True

    def already_spawned(env: Mapping[str, str]) -> bool:
        return True

    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_memory", record_spawn)
    monkeypatch.setattr(
        "saddlebag.agents.claude_code.hook.spawn_ingest", already_spawned
    )
    monkeypatch.setattr(
        "saddlebag.agents.claude_code.hook.spawn_process", already_spawned
    )
    monkeypatch.setattr(
        "saddlebag.agents.claude_code.hook.spawn_transcripts", already_spawned
    )
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))

    assert hook.main() == 0
    assert len(spawned) == 1


def test_session_start_still_prints_nothing_when_the_sync_cannot_spawn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail-soft is not weakened by adding a third spawn."""
    from saddlebag.agents.claude_code import hook

    def boom(*a: object, **k: object) -> None:
        raise OSError("no processes")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""
