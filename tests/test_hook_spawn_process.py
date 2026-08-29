"""The SessionStart hook's detached `remem events process`.

Any session works off the backlog, which is what keeps extraction from being
stranded by the session that produced the events having ended. It must never
block the session and must never print into it.
"""

import subprocess

from remem.agents.claude_code import hook


def test_spawn_process_launches_a_detached_run(monkeypatch):
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert hook.spawn_process({"PATH": "/usr/bin"}) is True
    assert seen["cmd"][:3] == ["remem", "events", "process"]


def test_spawn_process_does_not_block_on_the_child(monkeypatch):
    """A session must not wait for extraction to finish."""
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    hook.spawn_process({})
    assert seen.get("stdout") == subprocess.DEVNULL
    assert seen.get("stderr") == subprocess.DEVNULL
    assert seen.get("start_new_session") is True


def test_spawn_process_is_skipped_inside_an_extraction_child(monkeypatch):
    called = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: called.append(1))
    assert hook.spawn_process({"REMEM_CAPTURE_CHILD": "1"}) is False
    assert called == []


def test_spawn_process_returns_false_when_remem_is_missing(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("remem")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert hook.spawn_process({}) is False


def test_session_start_still_prints_nothing_when_spawning_fails(
    monkeypatch, capsys
):
    def boom(*a, **k):
        raise OSError("no processes")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""
