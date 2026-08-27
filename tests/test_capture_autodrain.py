import subprocess

from remem.agents.claude_code import hook


def test_spawn_drain_launches_a_detached_drain(monkeypatch):
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert hook.spawn_drain({"PATH": "/usr/bin"}) is True
    assert seen["cmd"][:3] == ["remem", "capture", "drain"]


def test_spawn_drain_does_not_block_on_the_child(monkeypatch):
    """A session must not wait for distillation to finish."""
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    hook.spawn_drain({})
    assert seen.get("stdout") == subprocess.DEVNULL
    assert seen.get("stderr") == subprocess.DEVNULL
    assert seen.get("start_new_session") is True


def test_spawn_drain_is_skipped_inside_a_capture_child(monkeypatch):
    called = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: called.append(1))
    assert hook.spawn_drain({"REMEM_CAPTURE_CHILD": "1"}) is False
    assert called == []


def test_spawn_drain_returns_false_when_remem_is_missing(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("remem")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert hook.spawn_drain({}) is False


def test_session_start_still_prints_nothing_when_spawning_fails(
    monkeypatch, capsys
):
    def boom(*a, **k):
        raise OSError("no processes")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""
