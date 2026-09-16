"""CLI tests for `bag transcripts discover|designate|import|refresh`.

Same bootstrap as tests/test_memory_cli.py and tests/test_memory_refresh_cli.py:
these commands commit through `open_session()`, so the schema has to be
committed before the CLI connects, and the DSN is threaded through the
environment rather than a fixture object the CLI can see directly.

`status` is deliberately absent - its service function (`transcripts.status`)
does not exist until Task 10, so the command lands there, beside what it
calls.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.cli import app
from saddlebag.domain import Event, EventKind, new_id
from saddlebag.project import resolve_project

pytestmark = pytest.mark.db

runner = CliRunner()

#: `env` fixtures elsewhere in this repo resolve `--project`-less commands
#: the same way the CLI does: from the real working directory, via git.
#: Computed once so the fixture that seeds recorded events and the CLI
#: invocation cannot disagree about which project they mean.
PROJECT = resolve_project() or "saddlebag"


@pytest.fixture
def cli_env(live_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Points BAG_DSN at the scratch database, mints the owner, and seeds a
    transcript directory discovery can find under a redirected `Path.home()`.

    `discover` (and, later, `status`) default their `root` to
    `Path.home() / ".claude" / "projects"`, exactly how Claude Code lays
    transcripts out for real - so `Path.home` is patched, the way
    `tests/test_cli.py`'s install test already does, rather than passing a
    root the CLI cannot see any other way.

    Three files are seeded, two of whose session ids have a recorded event
    for `PROJECT` - the third is what keeps `matched` and `total` from being
    the same number, which is the entire point of the discover test: the
    directory's total file count is what a claim commits someone to reading,
    not the smaller count that merely proves ownership.
    """
    monkeypatch.setenv("BAG_DSN", live_dsn)
    monkeypatch.setenv("BAG_USER_ID", "brandon")
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()

    conn: psycopg.Connection[Any] = psycopg.connect(live_dsn)
    store = PostgresStore(conn)
    owner = store.ensure_principal("brandon")
    for session_id in ("sess-1", "sess-2"):
        store.put_event(
            Event(
                id=new_id(),
                owner_id=owner.id,
                project=PROJECT,
                harness="claude-code",
                session_id=session_id,
                kind=EventKind.TOOL_CALL,
                tool="Bash",
                payload={"command": "ls"},
                occurred_at=datetime(2026, 9, 15, tzinfo=UTC),
            )
        )
    conn.commit()
    conn.close()

    directory = tmp_path / ".claude" / "projects" / "-old-dirname"
    directory.mkdir(parents=True)
    for session_id in ("sess-1", "sess-2", "never-recorded"):
        (directory / f"{session_id}.jsonl").write_bytes(
            json.dumps({"type": "user"}).encode() + b"\n"
        )


def test_discover_prints_its_evidence_and_writes_nothing(
    cli_env: None, tmp_path: Path
) -> None:
    """The counts are the point: claiming imports `total`, not `matched`."""
    result = runner.invoke(app, ["transcripts", "discover"])
    assert result.exit_code == 0
    assert "2 of 3" in result.stdout


def test_designate_echoes_the_backfill_command_and_its_cost(
    cli_env: None, tmp_path: Path
) -> None:
    """Claiming must not silently commit someone to a large read.

    The command records a claim and imports nothing, so the output has to
    name what comes next and roughly what it will cost.
    """
    result = runner.invoke(app, ["transcripts", "designate", str(tmp_path)])
    assert result.exit_code == 0
    assert "bag transcripts import" in result.stdout


def test_designate_exits_non_zero_when_refused(cli_env: None) -> None:
    result = runner.invoke(app, ["transcripts", "designate", "/does/not/exist"])
    assert result.exit_code == 1
    assert "does not exist" in result.stdout


def test_refresh_exits_zero_and_prints_nothing_on_stdout(cli_env: None) -> None:
    """Fail-soft, like every spawned half in this repo."""
    result = runner.invoke(app, ["transcripts", "refresh"])
    assert result.exit_code == 0
    assert result.stdout == ""


def test_refresh_exits_zero_with_an_unreachable_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_session` raises typer.Exit(1) here, which must not escape."""
    monkeypatch.setenv("BAG_DSN", "postgresql://nobody@127.0.0.1:1/none")
    result = runner.invoke(app, ["transcripts", "refresh"])
    assert result.exit_code == 0


def test_refresh_survives_a_library_calling_sys_exit(
    monkeypatch: pytest.MonkeyPatch, cli_env: None
) -> None:
    """What the `except BaseException` is actually for.

    The unreachable-database case above passes under `except Exception` too,
    because typer.Exit subclasses RuntimeError - so it proves nothing about
    this guard. A genuine SystemExit does.
    """
    from saddlebag.services import transcripts

    def boom(*args: object, **kwargs: object) -> None:
        raise SystemExit(3)

    monkeypatch.setattr(transcripts, "run", boom)
    result = runner.invoke(app, ["transcripts", "refresh"])
    assert result.exit_code == 0
