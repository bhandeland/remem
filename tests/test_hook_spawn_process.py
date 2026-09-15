"""The SessionStart hook's detached `bag events process`.

Any session works off the backlog, which is what keeps extraction from being
stranded by the session that produced the events having ended. It must never
block the session and must never print into it.
"""

import subprocess
from typing import Any

import pytest

from saddlebag.agents.claude_code import hook


def test_spawn_process_launches_a_detached_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_popen(cmd: list[str], **kwargs: object) -> object:
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert hook.spawn_process({"PATH": "/usr/bin"}) is True
    assert seen["cmd"][:3] == ["bag", "events", "process"]


def test_spawn_process_does_not_block_on_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session must not wait for extraction to finish."""
    seen: dict[str, Any] = {}

    def fake_popen(cmd: list[str], **kwargs: object) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    hook.spawn_process({})
    assert seen.get("stdout") == subprocess.DEVNULL
    assert seen.get("stderr") == subprocess.DEVNULL
    assert seen.get("start_new_session") is True


def test_spawn_process_is_skipped_inside_an_extraction_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[int] = []

    def fake_popen(*a: object, **k: object) -> None:
        called.append(1)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert hook.spawn_process({"BAG_EXTRACT_CHILD": "1"}) is False
    assert called == []


def test_spawn_process_returns_false_when_saddlebag_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise FileNotFoundError("bag")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert hook.spawn_process({}) is False


def test_session_start_still_prints_nothing_when_spawning_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise OSError("no processes")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""
