"""The SessionEnd hook, which no longer queues anything.

Extraction runs on an idle timer, so a session ending is not what starts it.
The command stays registered because an installed settings.json names it, and
a hook command that does not exist is an error on every session close - so
what is asserted here is that it stays silent, exits zero, and writes nothing.
Task 9 gives it a job again: recording a `session_end` event, which shortens
the idle wait without being required by it.
"""

import io
import json

import psycopg
import pytest

from remem.agents.claude_code import hook
from remem.backends.postgres.migrate import migrate


def _payload(cwd, transcript, session_id="sess-1"):
    return json.dumps(
        {"cwd": cwd, "transcript_path": transcript, "session_id": session_id}
    )


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


def test_main_session_end_exits_zero_when_stdin_raises(monkeypatch):
    class Exploding:
        def read(self):
            raise OSError("gone")

    monkeypatch.setattr("sys.stdin", Exploding())
    assert hook.main_session_end() == 0


@pytest.mark.db
def test_session_end_writes_nothing(live_dsn, tmp_path):
    """The idle trigger replaced the end hook. Nothing is queued, and no
    event is recorded here either - that is Task 9's PostToolUse path."""
    project_dir = tmp_path / "myproj"
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
        assert c.execute("select count(*) from events").fetchone()[0] == 0
        assert c.execute(
            "select count(*) from extract_jobs"
        ).fetchone()[0] == 0
