"""The PostToolUse/SessionEnd recording hook.

`record_event` is what both `remem hook record-event` (PostToolUse) and
`remem hook session-end` now run - `ClaudeCodeAdapter.event()` maps both
payload shapes to the same kinds, so one function and one command name
serves both. The idle trigger means SessionEnd is a hint that shortens the
wait, not a requirement: a harness with no SessionEnd hook loses nothing but
time, which is what this file's SessionEnd test is checking - that recording
still happens, and that nothing is enqueued the way the old capture job used
to be.
"""

import io
import json

import psycopg
import pytest

from remem.agents.claude_code import hook
from remem.backends.postgres.migrate import migrate
from remem.extract.base import CHILD_ENV_VAR


def _payload(cwd, hook_event_name="PostToolUse", session_id="sess-1", **overrides):
    payload = {
        "cwd": cwd,
        "hook_event_name": hook_event_name,
        "session_id": session_id,
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_record_event_is_silent_on_malformed_stdin(capsys):
    hook.record_event("{not json", env={})
    assert capsys.readouterr().out == ""


def test_the_hook_exits_zero_when_the_database_is_unreachable(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(_payload("/tmp/whatever")),
    )
    monkeypatch.setenv("REMEM_DSN", "postgresql://nobody@127.0.0.1:1/none")
    assert hook.main_record_event() == 0
    assert capsys.readouterr().out == ""


def test_the_hook_refuses_to_recurse_inside_an_extraction_child(monkeypatch, capsys):
    """CHILD_ENV_VAR is checked by every hook. The extractor spawns
    `claude -p`, whose own hooks would otherwise record the extraction as
    events, which the next extraction would then read - an unbounded loop.
    """
    calls = []
    monkeypatch.setattr(
        "remem.agents.claude_code.adapter.ClaudeCodeAdapter.event",
        lambda self, env, payload: calls.append(1),
    )
    hook.record_event(
        _payload("/tmp/whatever"), env={CHILD_ENV_VAR: "1", "REMEM_HOOK_DEBUG": "1"}
    )
    assert calls == []
    assert "extraction child" in capsys.readouterr().err


def test_main_record_event_always_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
    assert hook.main_record_event() == 0
    assert capsys.readouterr().out == ""


def test_main_record_event_exits_zero_when_stdin_raises(monkeypatch):
    class Exploding:
        def read(self):
            raise OSError("gone")

    monkeypatch.setattr("sys.stdin", Exploding())
    assert hook.main_record_event() == 0


def test_debug_explains_an_unreachable_database_on_stderr(capsys):
    """Stdout is reserved for real hook output; an explanation only ever
    goes to stderr, and only under REMEM_HOOK_DEBUG."""
    hook.record_event(
        _payload("/tmp/whatever"),
        env={
            "REMEM_DSN": "postgresql://nobody@127.0.0.1:1/none",
            "REMEM_HOOK_DEBUG": "1",
        },
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "remem hook" in captured.err


def test_debug_is_silent_unless_asked_for(capsys):
    hook.record_event(
        _payload("/tmp/whatever"),
        env={"REMEM_DSN": "postgresql://nobody@127.0.0.1:1/none"},
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.db
def test_the_session_end_hook_records_an_event_and_enqueues_nothing(
    live_dsn, tmp_path
):
    """The idle rule replaced the end hook as the trigger. SessionEnd is now
    a hint that shortens the wait, and a harness without one loses nothing
    but time - what it must still do is record, and it must not enqueue
    the retired capture job the way it used to."""
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()

    env = {
        "REMEM_DSN": live_dsn,
        "REMEM_USER_ID": "brandon",
        "REMEM_CONFIG": str(tmp_path / "none.toml"),
    }

    from remem.backends.postgres.store import PostgresStore
    from remem.services import record

    with psycopg.connect(live_dsn) as c:
        owner = PostgresStore(c).ensure_principal("brandon")
        record.enable(PostgresStore(c), owner.id, "myproj")
        c.commit()

    hook.record_event(
        _payload(str(project_dir), hook_event_name="SessionEnd"), env=env
    )

    with psycopg.connect(live_dsn) as c:
        rows = c.execute(
            "select kind from events"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "session_end"
        assert c.execute(
            "select count(*) from extract_jobs"
        ).fetchone()[0] == 0
        assert c.execute(
            "select count(*) from capture_jobs_legacy"
        ).fetchone()[0] == 0
