import io
import json

import psycopg
import pytest

from remem.agents.claude_code import hook
from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.services import capture


def _payload(cwd, transcript, session_id="sess-1"):
    return json.dumps(
        {"cwd": cwd, "transcript_path": transcript, "session_id": session_id}
    )


# --- fail-soft, no database needed ------------------------------------------


def test_session_end_is_silent_on_malformed_stdin(capsys):
    hook.session_end("{not json", env={})
    assert capsys.readouterr().out == ""


def test_session_end_is_silent_with_an_unreachable_database(capsys, tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text("{}")
    hook.session_end(
        _payload(str(tmp_path), str(t)),
        env={"REMEM_DSN": "postgresql://nobody@127.0.0.1:1/none"},
    )
    assert capsys.readouterr().out == ""


def test_main_session_end_always_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
    assert hook.main_session_end() == 0
    assert capsys.readouterr().out == ""


def test_main_session_end_exits_zero_when_stdin_raises(monkeypatch, capsys):
    class Exploding:
        def read(self):
            raise OSError("gone")

    monkeypatch.setattr("sys.stdin", Exploding())
    assert hook.main_session_end() == 0


def test_the_recursion_guard_stops_the_hook_before_any_work(monkeypatch, tmp_path):
    """claude -p starts a session whose SessionEnd hook would enqueue another
    job, spawning another claude, without bound."""
    called = []
    monkeypatch.setattr(
        capture, "enqueue", lambda *a, **k: called.append(1)
    )
    t = tmp_path / "t.jsonl"
    t.write_text("{}")
    hook.session_end(
        _payload(str(tmp_path), str(t)), env={"REMEM_CAPTURE_CHILD": "1"}
    )
    assert called == []


# --- enqueueing -------------------------------------------------------------


@pytest.mark.db
def test_session_end_enqueues_when_capture_is_enabled(
    live_dsn, tmp_path, monkeypatch
):
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}")

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        store = PostgresStore(c)
        owner = store.ensure_principal("brandon")
        capture.enable(store, owner.id, "myproj")
        c.commit()

    env = {"REMEM_DSN": live_dsn, "REMEM_USER_ID": "brandon",
           "REMEM_CONFIG": str(tmp_path / "none.toml")}
    hook.session_end(_payload(str(project_dir), str(transcript)), env=env)

    with psycopg.connect(live_dsn) as c:
        rows = c.execute("select project, session_id from capture_jobs").fetchall()
    assert rows == [("myproj", "sess-1")]


@pytest.mark.db
def test_session_end_enqueues_nothing_when_capture_is_disabled(
    live_dsn, tmp_path
):
    project_dir = tmp_path / "otherproj"
    project_dir.mkdir()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}")

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()

    env = {"REMEM_DSN": live_dsn, "REMEM_USER_ID": "brandon",
           "REMEM_CONFIG": str(tmp_path / "none.toml")}
    hook.session_end(_payload(str(project_dir), str(transcript)), env=env)

    with psycopg.connect(live_dsn) as c:
        count = c.execute("select count(*) from capture_jobs").fetchone()[0]
    assert count == 0


@pytest.mark.db
def test_session_end_ignores_a_payload_without_a_transcript(live_dsn, tmp_path):
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    env = {"REMEM_DSN": live_dsn, "REMEM_USER_ID": "brandon",
           "REMEM_CONFIG": str(tmp_path / "none.toml")}
    hook.session_end(json.dumps({"cwd": str(tmp_path)}), env=env)
    with psycopg.connect(live_dsn) as c:
        assert c.execute("select count(*) from capture_jobs").fetchone()[0] == 0
