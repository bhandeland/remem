"""The session-start hook's detached `remem memory refresh`.

The third sibling of `spawn_process` and `spawn_ingest`, making the same
three promises: never block the session, never print into it, never recurse
into the extractor's own child. Sync was manual for the life of the
feature, and `memory_runs` was built one branch ahead of this caller - the
one with no terminal to report to.
"""

import subprocess

from remem import hookio
from remem.extract.base import CHILD_ENV_VAR


def test_spawn_memory_launches_the_refresh(monkeypatch):
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert hookio.spawn_memory({"PATH": "/usr/bin"}) is True
    assert seen["cmd"][:3] == ["remem", "memory", "refresh"]


def test_spawn_memory_does_not_block_on_the_child(monkeypatch):
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    hookio.spawn_memory({})
    assert seen.get("stdout") == subprocess.DEVNULL
    assert seen.get("stderr") == subprocess.DEVNULL
    assert seen.get("start_new_session") is True


def test_spawn_memory_is_skipped_inside_an_extraction_child(monkeypatch):
    """The extractor's `claude -p` runs remem's hooks. It must not sync.

    Same guard, same reason, as its two siblings: an extraction child that
    spawns work which the next extraction reads has no bound. Sync is the
    worst of the three to leave unguarded, because it writes files into a
    directory Claude Code is itself writing.
    """
    called = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: called.append(1))
    assert hookio.spawn_memory({CHILD_ENV_VAR: "1"}) is False
    assert called == []


def test_spawn_memory_returns_false_when_remem_is_missing(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("remem")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert hookio.spawn_memory({}) is False


# --- both trigger points, because there are two and only two -----------
def test_session_start_spawns_the_sync(monkeypatch):
    from remem.agents.claude_code import hook

    spawned = []
    monkeypatch.setattr(
        "remem.agents.claude_code.hook.spawn_memory",
        lambda env: spawned.append(env) or True,
    )
    monkeypatch.setattr("remem.agents.claude_code.hook.spawn_ingest", lambda env: True)
    monkeypatch.setattr("remem.agents.claude_code.hook.spawn_process", lambda env: True)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))

    assert hook.main() == 0
    assert len(spawned) == 1


def test_session_start_still_prints_nothing_when_the_sync_cannot_spawn(
    monkeypatch, capsys
):
    """Fail-soft is not weakened by adding a third spawn."""
    from remem.agents.claude_code import hook

    def boom(*a, **k):
        raise OSError("no processes")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""
