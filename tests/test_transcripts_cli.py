"""CLI tests for `bag transcripts
discover|designate|undesignate|import|refresh|status`.

Same bootstrap as tests/test_memory_cli.py and tests/test_memory_refresh_cli.py:
these commands commit through `open_session()`, so the schema has to be
committed before the CLI connects, and the DSN is threaded through the
environment rather than a fixture object the CLI can see directly.

`status` lands here in Task 10, alongside the service function
(`transcripts.status`) it calls - it could not exist any earlier.
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

    `discover` (and `status`) default their `root` to
    `transcripts.transcript_root()`, which is `~/.claude/projects` unless
    `CLAUDE_CONFIG_DIR` moves it - exactly how Claude Code lays transcripts
    out for real. So `Path.home` is patched, the way `tests/test_cli.py`'s
    install test already does, rather than passing a root the CLI cannot
    see any other way, and `CLAUDE_CONFIG_DIR` is cleared: it is a real
    variable a developer may have set, and leaving it would point these
    tests at that machine's directory instead of `tmp_path`.

    Three files are seeded, two of whose session ids have a recorded event
    for `PROJECT` - the third is what keeps `matched` and `total` from being
    the same number, which is the entire point of the discover test: the
    directory's total file count is what a claim commits someone to reading,
    not the smaller count that merely proves ownership. One subagent file is
    also seeded, so every command that describes this directory has to say
    something about the files a claim brings beyond sessions.
    """
    monkeypatch.setenv("BAG_DSN", live_dsn)
    monkeypatch.setenv("BAG_USER_ID", "brandon")
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    def _home(cls: type[pathlib.Path]) -> pathlib.Path:
        return tmp_path

    monkeypatch.setattr(pathlib.Path, "home", classmethod(_home))

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

    # One subagent file, so every command that describes this directory
    # has to say something about the files a claim brings beyond sessions.
    subagent = directory / "sess-1" / "subagents" / "agent-a1.jsonl"
    subagent.parent.mkdir(parents=True)
    subagent.write_bytes(json.dumps({"type": "user"}).encode() + b"\n")


def test_discover_prints_its_evidence_and_writes_nothing(
    cli_env: None, tmp_path: Path
) -> None:
    """The counts are the point: claiming imports `total`, not `matched`."""
    result = runner.invoke(app, ["transcripts", "discover"])
    assert result.exit_code == 0
    assert "2 of 3" in result.stdout
    assert "plus 1 subagent files" in result.stdout


def test_discover_looks_under_claude_config_dir_when_it_is_set(
    cli_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CLAUDE_CONFIG_DIR moves the whole tree, transcripts included.

    Every other place this repo touches that tree honours the override, and
    the two hand-built `~/.claude/projects` paths in `cli.py` did not: for
    anyone who sets it, `discover` proposed nothing with no evidence and no
    error, and `status` called every recorded session irrecoverable.

    The fixture's own directory under the patched home is deliberately left
    in place and NOT what is asserted on. Asserting only "something was
    found" would pass on the hardcoded path too - it is finding the
    directory that exists ONLY under the override that tells the two
    implementations apart.
    """
    elsewhere = tmp_path / "xdg-ish" / "claude-config"
    directory = elsewhere / "projects" / "-moved-config"
    directory.mkdir(parents=True)
    (directory / "sess-1.jsonl").write_bytes(json.dumps({"type": "user"}).encode())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(elsewhere))

    result = runner.invoke(app, ["transcripts", "discover"])

    assert result.exit_code == 0
    assert str(directory) in result.stdout


def test_designate_echoes_the_backfill_command_and_its_cost(
    cli_env: None, tmp_path: Path
) -> None:
    """Claiming must not silently commit someone to a large read.

    The command records a claim and imports nothing, so the output has to
    name what comes next and roughly what it will cost.
    """
    directory = tmp_path / ".claude" / "projects" / "-old-dirname"
    result = runner.invoke(app, ["transcripts", "designate", str(directory)])
    assert result.exit_code == 0
    assert "bag transcripts import" in result.stdout
    assert "3 sessions, 1 subagent files" in result.stdout


def test_undesignate_releases_a_claim_and_keeps_what_was_imported(
    cli_env: None, tmp_path: Path
) -> None:
    """The way out of a claim made under the wrong project.

    `designate` refuses a directory another project holds, so without this
    the only release was hand-written SQL. The echo has to say that stored
    transcripts survive: a user reaching for this has usually just been told
    their sessions are filed under the wrong project, and needs to know that
    releasing the claim is not a delete.
    """
    assert (
        runner.invoke(app, ["transcripts", "designate", str(tmp_path)]).exit_code == 0
    )

    result = runner.invoke(app, ["transcripts", "undesignate", str(tmp_path)])

    assert result.exit_code == 0
    assert "kept" in result.stdout
    after = runner.invoke(app, ["transcripts", "status"])
    assert "no directory claimed" in after.stdout


def test_undesignate_exits_non_zero_when_nothing_was_claimed(
    cli_env: None, tmp_path: Path
) -> None:
    """A person typed this, so "not claimed" is said, not swallowed."""
    result = runner.invoke(app, ["transcripts", "undesignate", str(tmp_path)])
    assert result.exit_code == 1
    assert "does not claim" in result.stdout


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


def test_import_records_a_crashed_run_rather_than_losing_it(
    cli_env: None, monkeypatch: pytest.MonkeyPatch, live_dsn: str, tmp_path: Path
) -> None:
    """Pins `autocommit=True` on `bag transcripts import`'s session.

    Without it, a mid-run failure poisons the ONE transaction the session
    holds open: `run()`'s `finally` then tries to UPDATE the started row,
    which raises `InFailedSqlTransaction` in place of the original error -
    and psycopg's own rollback on the way out discards the started row
    along with everything else. "Crashed" becomes indistinguishable from
    "never ran", which is the entire reason the row exists. With
    `autocommit=True` each statement is durable on its own, so the started
    row survives and the finishing UPDATE still runs after the failure.

    `transcripts.run` writes no row at all for a project with no claim, so
    this test claims a real directory first - otherwise the failure below
    would fire before a row was ever started, and the test would pass or
    fail independently of `autocommit`, proving nothing. It holds THREE
    files because one failing file is only that file's failure now; it takes
    `MAX_CONSECUTIVE_FILE_FAILURES` in a row to make the run raise.

    The store method below is monkeypatched to run genuinely bad SQL,
    rather than just raise a plain Python exception - only a real
    statement failure reproduces the poisoning above, which is exactly
    what this test needs to tell the two session modes apart.
    """
    from saddlebag.backends.postgres.store import PostgresStore

    claimed = tmp_path / "claimed"
    claimed.mkdir()
    for session_id in ("sess-1", "sess-2", "sess-3"):
        (claimed / f"{session_id}.jsonl").write_bytes(
            json.dumps({"type": "user"}).encode() + b"\n"
        )

    conn: psycopg.Connection[Any] = psycopg.connect(live_dsn)
    store = PostgresStore(conn)
    owner = store.ensure_principal("brandon")
    store.add_transcript_path(owner.id, PROJECT, str(claimed))
    conn.commit()
    conn.close()

    def boom(
        self: PostgresStore,
        owner_id: object,
        harness: object,
        session_id: object,
        agent_id: object = None,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute("select this_column_does_not_exist")
        return None  # unreachable - the execute above always raises

    monkeypatch.setattr(PostgresStore, "get_transcript", boom)

    result = runner.invoke(app, ["transcripts", "import"])
    assert result.exit_code != 0

    conn = psycopg.connect(live_dsn)
    store = PostgresStore(conn)
    owner = store.ensure_principal("brandon")
    run = store.latest_transcript_run(owner.id, PROJECT)
    conn.close()

    assert run is not None
    assert run.finished_at is not None
    assert any(f.get("path") == "*" for f in run.failures)


def test_status_json_has_the_same_keys_when_nothing_is_claimed(cli_env: None) -> None:
    """One object, not a list, and never a shorter document.

    A consumer checks a field for null rather than branching on which keys
    arrived - the same rule `bag memory status --json` follows, and
    deliberately not `bag reingest status --json`, which sweeps every project
    and so returns an array.
    """
    result = runner.invoke(app, ["transcripts", "status", "--json"])
    assert result.exit_code == 0
    got = json.loads(result.stdout)
    assert set(got) == {
        "project",
        "paths",
        "run",
        "backlog",
        "subagent_backlog",
        "meta_backlog",
        "irrecoverable",
    }
    assert got["run"] is None


def test_status_prints_subagent_figures_beside_session_ones(
    cli_env: None, tmp_path: Path
) -> None:
    directory = tmp_path / ".claude" / "projects" / "-old-dirname"
    assert (
        runner.invoke(app, ["transcripts", "designate", str(directory)]).exit_code == 0
    )
    result = runner.invoke(app, ["transcripts", "status"])
    assert result.exit_code == 0
    assert "3 sessions, 1 subagent files" in result.stdout
    assert "backlog: 3 sessions, 1 subagent files" in result.stdout


def test_import_and_status_print_the_sidecar_figures(
    cli_env: None, tmp_path: Path
) -> None:
    directory = tmp_path / ".claude" / "projects" / "-old-dirname"
    (directory / "sess-1" / "subagents" / "agent-a1.meta.json").write_bytes(
        b'{"agentType":"implementer"}'
    )
    assert (
        runner.invoke(app, ["transcripts", "designate", str(directory)]).exit_code == 0
    )
    before = runner.invoke(app, ["transcripts", "status"])
    # Not stored yet: the subagent is backlog, so its sidecar is not counted.
    assert "backlog: 3 sessions, 1 subagent files, 0 sidecars" in before.stdout

    result = runner.invoke(app, ["transcripts", "import"])
    assert result.exit_code == 0
    assert "1 sidecars" in result.stdout


def test_a_real_statement_failure_costs_only_its_own_file(
    cli_env: None, monkeypatch: pytest.MonkeyPatch, live_dsn: str, tmp_path: Path
) -> None:
    """Under autocommit a failed statement does not poison the connection,
    which is what lets the files after it import. Only real SQL shows that:
    a Python exception never touches the connection."""
    from saddlebag.backends.postgres.store import PostgresStore

    claimed = tmp_path / "claimed"
    claimed.mkdir()
    for session_id in ("sess-bad", "sess-good"):
        (claimed / f"{session_id}.jsonl").write_bytes(
            json.dumps({"type": "user"}).encode() + b"\n"
        )
    conn: psycopg.Connection[Any] = psycopg.connect(live_dsn)
    store = PostgresStore(conn)
    owner = store.ensure_principal("brandon")
    store.add_transcript_path(owner.id, PROJECT, str(claimed))
    conn.commit()
    conn.close()

    real = PostgresStore.get_transcript

    def bad_for_one(
        self: PostgresStore,
        owner_id: Any,
        harness: str,
        session_id: str,
        agent_id: Any = None,
    ) -> Any:
        if session_id == "sess-bad":
            with self._conn.cursor() as cur:
                cur.execute("select this_column_does_not_exist")
        return real(self, owner_id, harness, session_id, agent_id)

    monkeypatch.setattr(PostgresStore, "get_transcript", bad_for_one)

    result = runner.invoke(app, ["transcripts", "import"])
    assert result.exit_code == 1
    assert "sess-bad.jsonl: UndefinedColumn" in result.stderr

    monkeypatch.undo()
    conn = psycopg.connect(live_dsn)
    store = PostgresStore(conn)
    owner = store.ensure_principal("brandon")
    got = store.get_transcript(owner.id, "claude-code", "sess-good")
    run = store.latest_transcript_run(owner.id, PROJECT)
    conn.close()
    assert got is not None
    assert run is not None
    assert run.files_new == 1
