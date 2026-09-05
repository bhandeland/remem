import io
import json

import pytest

from remem.agents.claude_code.hook import main, session_start
from remem.backends.postgres.migrate import migrate


def test_returns_empty_when_the_database_is_unreachable():
    payload = json.dumps({"cwd": "/tmp/whatever", "session_id": "s1"})
    out = session_start(
        payload, env={"REMEM_DSN": "postgresql://nobody@127.0.0.1:1/none"}
    )
    assert out == ""


def test_returns_empty_on_malformed_stdin():
    assert session_start("{not json", env={}) == ""


def test_returns_empty_on_empty_stdin():
    assert session_start("", env={}) == ""


def test_main_exits_zero_when_the_database_is_unreachable(monkeypatch, capsys):
    monkeypatch.setenv("REMEM_DSN", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"cwd": "/tmp/x"}))
    )
    assert main() == 0
    assert capsys.readouterr().out == ""


def test_main_exits_zero_on_garbage_input(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
    assert main() == 0
    assert capsys.readouterr().out == ""


@pytest.mark.db
def test_injects_the_project_knowledge_base(live_dsn, tmp_path, monkeypatch):
    import psycopg

    from remem.services import kb
    from remem.services.write import remember
    from remem.session import open_session

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()

    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))

    project_dir = tmp_path / "myproj"
    project_dir.mkdir()

    with open_session() as s:
        from remem.domain import CollectionQuery, Kind
        kb.create(s.store, s.owner.id, slug="myproj", title="myproj",
                  query=CollectionQuery(project="myproj"))
        remember(s.store, s.owner.id, title="Lint rule",
                 body="always run ruff", summary="Run ruff linter",
                 kind=Kind.RULE, project="myproj")
        s.conn.commit()

    out = session_start(
        json.dumps({"cwd": str(project_dir), "session_id": "s1"}),
        env={"REMEM_DSN": live_dsn, "REMEM_USER_ID": "brandon",
             "REMEM_CONFIG": str(tmp_path / "none.toml")},
    )
    assert "Run ruff linter" in out


@pytest.mark.db
def test_returns_empty_when_the_project_has_no_knowledge_base(
    live_dsn, tmp_path, monkeypatch
):
    import psycopg

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()

    env = {"REMEM_DSN": live_dsn, "REMEM_USER_ID": "brandon",
           "REMEM_CONFIG": str(tmp_path / "none.toml")}
    out = session_start(json.dumps({"cwd": str(tmp_path / "unknown-proj")}), env=env)
    assert out == ""


def test_debug_is_silent_unless_asked_for(capsys):
    payload = json.dumps({"cwd": "/tmp/whatever", "session_id": "s1"})
    out = session_start(
        payload, env={"REMEM_DSN": "postgresql://nobody@127.0.0.1:1/none"}
    )
    captured = capsys.readouterr()
    assert out == ""
    assert captured.out == ""
    assert captured.err == ""


def test_main_is_silent_on_both_streams_by_default(monkeypatch, capsys):
    monkeypatch.delenv("REMEM_HOOK_DEBUG", raising=False)
    monkeypatch.setenv("REMEM_DSN", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": "/tmp/x"})))
    assert main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_debug_explains_an_unreachable_database_on_stderr(capsys):
    payload = json.dumps({"cwd": "/tmp/whatever", "session_id": "s1"})
    out = session_start(
        payload,
        env={"REMEM_DSN": "postgresql://nobody@127.0.0.1:1/none",
             "REMEM_HOOK_DEBUG": "1"},
    )
    captured = capsys.readouterr()
    assert out == ""
    assert captured.out == ""
    assert "remem hook" in captured.err


def test_debug_never_breaks_fail_soft(capsys):
    """Even if writing the diagnostic blows up, the hook still returns ""."""
    class Exploding(dict):
        def get(self, key, default=None):
            raise RuntimeError("boom")

    assert session_start(json.dumps({"cwd": "/tmp/x"}), env=Exploding()) == ""


@pytest.mark.db
def test_debug_names_the_missing_knowledge_base(live_dsn, tmp_path, capsys):
    import psycopg

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()

    env = {"REMEM_DSN": live_dsn, "REMEM_USER_ID": "brandon",
           "REMEM_CONFIG": str(tmp_path / "none.toml"), "REMEM_HOOK_DEBUG": "1"}
    out = session_start(json.dumps({"cwd": str(tmp_path / "unknown-proj")}), env=env)
    err = capsys.readouterr().err
    assert out == ""
    assert "unknown-proj" in err


def test_main_guard_survives_a_failure_inside_session_start(monkeypatch, capsys):
    """main() has its own try/except as a second layer. Prove it works.

    Both other main() tests pass even if this guard is deleted, because
    session_start's own guard already covers them. This one bypasses that by
    making session_start itself raise.
    """
    import remem.agents.claude_code.hook as hook

    def boom(*_args, **_kwargs):
        raise RuntimeError("session_start exploded")

    monkeypatch.setattr(hook, "session_start", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"cwd": "/tmp/x"}'))

    assert hook.main() == 0
    assert capsys.readouterr().out == ""


def test_main_returns_zero_when_stdin_itself_raises(monkeypatch, capsys):
    """A hook must exit 0 even if reading stdin fails."""
    import remem.agents.claude_code.hook as hook

    class ExplodingStdin:
        def read(self):
            raise OSError("stdin is gone")

    monkeypatch.setattr("sys.stdin", ExplodingStdin())
    assert hook.main() == 0
    assert capsys.readouterr().out == ""
