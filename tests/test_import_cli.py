"""CLI tests for `bag import claude-mem`."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.cli import app

pytestmark = pytest.mark.db

runner = CliRunner()

SCHEMA = """
create table projects (
  id text primary key, name text not null, slug text, root_path text,
  metadata text not null default '{}',
  created_at_epoch integer not null, updated_at_epoch integer not null
);
create table memory_items (
  id text primary key, project_id text not null, server_session_id text,
  legacy_observation_id integer, kind text not null, type text not null,
  title text, subtitle text, text text, narrative text,
  facts text not null default '[]', concepts text not null default '[]',
  files_read text not null default '[]',
  files_modified text not null default '[]',
  metadata text not null default '{}',
  created_at_epoch integer not null, updated_at_epoch integer not null
);
"""


@pytest.fixture
def env(live_dsn: str, tmp_path: Path) -> dict[str, str]:
    """CliRunner's `env=` merges into os.environ for the invoke call, which
    is what lets `_memory_dir`'s `Path.cwd()` call see CLAUDE_CONFIG_DIR
    pointed at tmp_path - so this is returned as a dict rather than set via
    monkeypatch, unlike test_ingest_cli.py's `env`, which never needs the
    claude-code adapter to resolve anything."""
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    return {
        "BAG_DSN": live_dsn,
        "BAG_USER_ID": "brandon",
        "BAG_CONFIG": str(tmp_path / "none.toml"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
    }


@pytest.fixture
def source(tmp_path: Path) -> Path:
    db = tmp_path / "claude-mem.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute(
        "insert into projects values (?,?,?,?,?,?,?)",
        ("p1", "at-workspace", "at-workspace", "/x", "{}", 1782832648, 1782832648),
    )
    conn.execute(
        "insert into memory_items values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "m1",
            "p1",
            "s1",
            None,
            "observation",
            "discovery",
            "A title",
            "A hook",
            None,
            "A narrative",
            json.dumps(["a fact"]),
            "[]",
            "[]",
            "[]",
            "{}",
            1782832648,
            1782832648,
        ),
    )
    conn.commit()
    conn.close()
    return db


def test_a_dry_run_reports_counts_and_writes_nothing(
    env: dict[str, str], source: Path
) -> None:
    result = runner.invoke(
        app, ["import", "claude-mem", str(source), "--dry-run"], env=env
    )

    assert result.exit_code == 0, result.output
    assert "at-workspace" in result.stdout
    assert "1" in result.stdout

    # The stdout-only assertions above would not catch --dry-run falling
    # through to a real write - Step 6 in the task brief proved exactly
    # that by breaking the wiring and watching this line, and only this
    # line, go red. Searching for the imported title is the CLI-level
    # check that a dry run really left the store untouched.
    search_result = runner.invoke(app, ["search", "A title", "--json"], env=env)
    assert json.loads(search_result.stdout) == []


def test_an_import_reports_what_it_created(env: dict[str, str], source: Path) -> None:
    result = runner.invoke(app, ["import", "claude-mem", str(source)], env=env)

    assert result.exit_code == 0, result.output
    assert "1 created" in result.stdout


def test_a_second_import_reports_nothing_changed(
    env: dict[str, str], source: Path
) -> None:
    runner.invoke(app, ["import", "claude-mem", str(source)], env=env)

    result = runner.invoke(app, ["import", "claude-mem", str(source)], env=env)

    assert "1 unchanged" in result.stdout


def test_a_file_that_is_not_a_claude_mem_database_exits_nonzero(
    env: dict[str, str], tmp_path: Path
) -> None:
    junk = tmp_path / "notes.txt"
    junk.write_text("not sqlite")

    result = runner.invoke(app, ["import", "claude-mem", str(junk)], env=env)

    assert result.exit_code == 1
    assert "notes.txt" in result.stderr


def test_a_missing_file_exits_nonzero(env: dict[str, str], tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["import", "claude-mem", str(tmp_path / "nope.db")], env=env
    )

    assert result.exit_code == 1


def test_an_unreadable_source_is_checked_before_a_session_opens(tmp_path: Path) -> None:
    """`claude_mem.read` must run before `_session()` connects - a file
    that is not a database should be refused without ever touching
    Postgres. Proved with an unreachable DSN: if the read happened after
    a connection attempt, this would fail with `_unreachable`'s message
    ("Cannot reach Postgres") instead of the reader's own. This is the
    guard test_a_file_that_is_not_a_claude_mem_database_exits_nonzero does
    NOT pin - that test still passes even when read() is moved inside
    the session block, because a real, reachable database is in `env`.
    """
    junk = tmp_path / "notes.txt"
    junk.write_text("not sqlite")
    env = {
        "BAG_DSN": "postgresql://nope:nope@localhost:1/nope",
        "BAG_USER_ID": "brandon",
        "BAG_CONFIG": str(tmp_path / "none.toml"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
    }

    result = runner.invoke(app, ["import", "claude-mem", str(junk)], env=env)

    assert result.exit_code == 1
    assert "notes.txt" in result.stderr
    assert "Cannot reach Postgres" not in result.stderr


def test_skipped_rows_are_reported(env: dict[str, str], tmp_path: Path) -> None:
    """A row `read()` cannot map is not silently dropped - the CLI must
    name it. Built with an unrecognised `kind` value, which is exactly what
    `ReadResult.skipped` exists to carry through from reader to report."""
    db = tmp_path / "claude-mem.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute(
        "insert into projects values (?,?,?,?,?,?,?)",
        ("p1", "at-workspace", "at-workspace", "/x", "{}", 1782832648, 1782832648),
    )
    conn.execute(
        "insert into memory_items values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "m2",
            "p1",
            "s1",
            None,
            "sketch",
            "discovery",
            "A title",
            "A hook",
            None,
            "A narrative",
            "[]",
            "[]",
            "[]",
            "[]",
            "{}",
            1782832648,
            1782832648,
        ),
    )
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["import", "claude-mem", str(db)], env=env)

    assert result.exit_code == 0, result.output
    assert "m2" in result.stdout
    assert "1 skipped" in result.stdout
