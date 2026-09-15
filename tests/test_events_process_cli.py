"""`bag events process` - the cron entry point.

Three things this command has to get right, and each of them is invisible
until it is wrong: it commits as it goes (a batch in one transaction discards
succeeded work when a later job errors the connection), it holds an advisory
lock (two overlapping cron runs would extract the same sessions twice), and
it says nothing at all when a run is already in flight.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.cli import app
from saddlebag.domain import Entry, Event, EventKind, Kind, new_id
from saddlebag.extract.base import ExtractedEntry
from saddlebag.services import record
from tests.conftest import one, scalar

runner = CliRunner()

pytestmark = pytest.mark.db

LONG_AGO = datetime.now(timezone.utc) - timedelta(hours=2)


@pytest.fixture
def env(live_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("BAG_DSN", live_dsn)
    monkeypatch.setenv("BAG_USER_ID", "brandon")
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))
    return live_dsn


def _record(
    dsn: str, project: str, session_id: str, command: str, when: datetime = LONG_AGO
):
    """Commit one event for a project that has opted in, and return the owner."""
    with psycopg.connect(dsn) as c:
        store = PostgresStore(c)
        owner = store.ensure_principal("brandon")
        record.enable(store, owner.id, project)
        store.put_event(
            Event(
                id=new_id(),
                owner_id=owner.id,
                project=project,
                harness="claude-code",
                session_id=session_id,
                kind=EventKind.TOOL_CALL,
                tool="Bash",
                payload={"command": command},
                occurred_at=when,
            )
        )
        c.commit()
        return owner.id


class _TitleFromFirstEvent:
    """Extracts one entry titled after the session's first command.

    Lets a test tell two sessions apart without spawning anything.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        pass

    def extract(
        self, events: list[Event], project: str, known_titles: list[str] | None = None
    ):
        return [
            ExtractedEntry(
                title=events[0].payload["command"], body="body", kind=Kind.NOTE
            )
        ]


def test_a_run_with_nothing_to_do_succeeds_quietly(env: str) -> None:
    result = runner.invoke(app, ["events", "process"])
    assert result.exit_code == 0
    assert "claimed 0" in result.stdout


def test_a_quiet_session_is_extracted(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(env, "saddlebag", "s1", "GOOD")
    monkeypatch.setattr(
        "saddlebag.extract.claude_cli.ClaudeCliExtractor", _TitleFromFirstEvent
    )

    result = runner.invoke(app, ["events", "process"])

    assert result.exit_code == 0
    assert "entries written 1" in result.stdout
    with psycopg.connect(env) as c:
        titles = [r[0] for r in c.execute("select title from entries").fetchall()]
    assert titles == ["GOOD"]


def test_a_session_inside_the_idle_window_is_left_alone(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BAG_IDLE_MINUTES is the trigger, and the CLI must pass it through
    rather than letting the service's own default decide."""
    _record(
        env,
        "saddlebag",
        "s1",
        "STILL-GOING",
        when=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    monkeypatch.setenv("BAG_IDLE_MINUTES", "20")
    monkeypatch.setattr(
        "saddlebag.extract.claude_cli.ClaudeCliExtractor", _TitleFromFirstEvent
    )

    result = runner.invoke(app, ["events", "process"])

    assert result.exit_code == 0
    assert "claimed 0" in result.stdout


def test_a_shorter_idle_window_makes_the_same_session_extractable(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(
        env,
        "saddlebag",
        "s1",
        "RECENT",
        when=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    monkeypatch.setenv("BAG_IDLE_MINUTES", "1")
    monkeypatch.setattr(
        "saddlebag.extract.claude_cli.ClaudeCliExtractor", _TitleFromFirstEvent
    )

    result = runner.invoke(app, ["events", "process"])

    assert "claimed 1" in result.stdout


def test_a_real_database_error_does_not_discard_the_rest_of_the_run(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One job erroring the connection must not roll back the whole batch.

    A genuine failed statement puts a non-autocommit connection into
    InFailedSqlTransaction, so every later statement - including the ones
    that record the failure - is swallowed, and Postgres turns the final
    COMMIT into a ROLLBACK without raising. The run then reports work it
    did not do. Unlike a wrapper raising a Python error, this touches the
    real connection, which is the only way to reproduce that state.
    """
    owner_id = _record(
        env, "saddlebag", "good", "GOOD", when=LONG_AGO - timedelta(minutes=5)
    )
    _record(env, "saddlebag", "bad", "BAD")

    real_put = PostgresStore.put_entry

    def flaky_put(self: PostgresStore, entry: Entry) -> Entry:
        if entry.title == "BAD":
            # A genuinely invalid statement on the live cursor, not a Python
            # exception raised beside it.
            self._conn.execute("select * from no_such_table_at_all")
        return real_put(self, entry)

    monkeypatch.setattr(PostgresStore, "put_entry", flaky_put)
    monkeypatch.setattr(
        "saddlebag.extract.claude_cli.ClaudeCliExtractor", _TitleFromFirstEvent
    )

    result = runner.invoke(app, ["events", "process"])
    assert result.exit_code == 0

    with psycopg.connect(env) as c:
        rows = {
            r[0]: (r[1], r[2])
            for r in c.execute(
                "select session_id, status, error from extract_jobs "
                "where owner_id = %s",
                (owner_id,),
            ).fetchall()
        }
        titles = [r[0] for r in c.execute("select title from entries").fetchall()]

    assert rows["good"][0] == "done"
    assert rows["bad"][0] == "failed"
    assert rows["bad"][1] is not None
    # The successful job's work survived its neighbour's failure.
    assert titles == ["GOOD"]
    # And the report told the truth about it.
    assert "entries written 1" in result.stdout


def test_the_job_flag_retries_one_job_that_gave_up(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_id = _record(env, "saddlebag", "s1", "GOOD")
    with psycopg.connect(env) as c:
        job_id = new_id()
        c.execute(
            "insert into extract_jobs (id, owner_id, project, harness, "
            "session_id, status, attempts, error) values "
            "(%s, %s, 'saddlebag', 'claude-code', 's1', 'failed', 9, "
            "'gave up after 3 attempts')",
            (job_id, owner_id),
        )
        c.commit()
    monkeypatch.setattr(
        "saddlebag.extract.claude_cli.ClaudeCliExtractor", _TitleFromFirstEvent
    )

    result = runner.invoke(app, ["events", "process", "--job", str(job_id)])

    assert result.exit_code == 0
    with psycopg.connect(env) as c:
        status, error = one(
            c.execute("select status, error from extract_jobs where id = %s", (job_id,))
        )
        titles = [r[0] for r in c.execute("select title from entries").fetchall()]
    assert status == "done"
    assert error is None
    assert titles == ["GOOD"]


def test_the_job_flag_with_an_unknown_id_exits_nonzero(env: str) -> None:
    result = runner.invoke(app, ["events", "process", "--job", str(uuid.uuid4())])
    assert result.exit_code == 1
    assert "No extract job" in result.stderr


def test_the_job_flag_with_a_malformed_id_exits_nonzero(env: str) -> None:
    result = runner.invoke(app, ["events", "process", "--job", "not-a-uuid"])
    assert result.exit_code == 1
    assert "not a valid" in result.stderr


def test_the_run_uses_the_configured_model(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI must pass config through, not let the extractor's default win."""
    seen = {}

    class Probe:
        def __init__(self, *args: Any, **kwargs: Any):
            seen.update(kwargs)

        def extract(
            self,
            events: list[Event],
            project: str,
            known_titles: list[str] | None = None,
        ) -> list[ExtractedEntry]:
            return []

    _record(env, "saddlebag", "s1", "anything")
    monkeypatch.setenv("BAG_CAPTURE_MODEL", "opus")
    monkeypatch.setattr("saddlebag.extract.claude_cli.ClaudeCliExtractor", Probe)

    assert runner.invoke(app, ["events", "process"]).exit_code == 0
    assert seen.get("model") == "opus"


def test_a_second_run_says_nothing_while_the_first_holds_the_lock(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cron overlap is the expected case, not an error. A non-zero exit here
    would mail the user about a working system."""
    owner_id = _record(env, "saddlebag", "s1", "GOOD")
    monkeypatch.setattr(
        "saddlebag.extract.claude_cli.ClaudeCliExtractor", _TitleFromFirstEvent
    )

    holder = psycopg.connect(env)
    try:
        held = scalar(
            holder.execute(
                "select pg_try_advisory_lock(hashtext('events-process'), hashtext(%s))",
                (str(owner_id),),
            )
        )
        assert held is True

        result = runner.invoke(app, ["events", "process"])

        assert result.exit_code == 0
        assert result.stdout == ""
        with psycopg.connect(env) as c:
            assert scalar(c.execute("select count(*) from entries")) == 0
    finally:
        holder.close()


def test_the_lock_is_released_when_the_run_ends(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Session-level, so it dies with the connection - which is the process
    ending. A lock that outlived one run would stop every later one."""
    _record(env, "saddlebag", "s1", "GOOD")
    monkeypatch.setattr(
        "saddlebag.extract.claude_cli.ClaudeCliExtractor", _TitleFromFirstEvent
    )

    assert runner.invoke(app, ["events", "process"]).exit_code == 0
    _record(env, "saddlebag", "s2", "SECOND")
    result = runner.invoke(app, ["events", "process"])

    assert "claimed 1" in result.stdout


def test_the_help_names_the_group(env: str) -> None:
    result = runner.invoke(app, ["events", "--help"])
    assert result.exit_code == 0
    assert "process" in result.stdout
