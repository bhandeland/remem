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
                 body="always run ruff", kind=Kind.RULE, project="myproj")
        s.conn.commit()

    out = session_start(
        json.dumps({"cwd": str(project_dir), "session_id": "s1"}),
        env={"REMEM_DSN": live_dsn, "REMEM_USER_ID": "brandon",
             "REMEM_CONFIG": str(tmp_path / "none.toml")},
    )
    assert "always run ruff" in out


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
