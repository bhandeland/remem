"""An unrecognised `kind` value skips its row rather than ending the import.

This is a one-time migration off a claude-mem installation whose version was
never verified in advance, so a `kind` string this build's `SourceKind` does
not know is plausible - a value added upstream between whenever these
fixtures were captured and whatever version actually produced the source
file. Refusing the whole import over one unclassifiable row would discard
every other row that read fine; this pins the alternative: drop the one row,
keep the rest.

The schema here is spelled literally, matching the memory_items shape a real
2026-06-30 export used, because this is the one place a `kind` column with
an out-of-vocabulary value can actually occur - the legacy `observations`
table (test_claude_mem_reader_legacy.py) has no `kind` column at all, so it
can never hit this path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from remem.importers.base import SourceKind
from remem.importers.claude_mem import read

MODERN_SCHEMA = """
create table projects (
  id text primary key, name text not null, slug text,
  root_path text, metadata text not null default '{}',
  created_at_epoch integer not null, updated_at_epoch integer not null
);
create table memory_items (
  id text primary key, project_id text not null, server_session_id text,
  legacy_observation_id integer,
  kind text not null, type text not null, title text, subtitle text,
  text text, narrative text,
  facts text not null default '[]', concepts text not null default '[]',
  files_read text not null default '[]',
  files_modified text not null default '[]',
  metadata text not null default '{}',
  created_at_epoch integer not null, updated_at_epoch integer not null
);
"""


def _db(tmp_path: Path, rows: list[tuple]) -> Path:
    db = tmp_path / "claude-mem.db"
    conn = sqlite3.connect(db)
    conn.executescript(MODERN_SCHEMA)
    conn.execute(
        "insert into projects values (?,?,?,?,?,?,?)",
        ("p1", "at-workspace", "at-workspace", "/x", "{}", 1782832648, 1782832648),
    )
    for row in rows:
        conn.execute(
            "insert into memory_items values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )
    conn.commit()
    conn.close()
    return db


def _row(item_id: str, kind: str) -> tuple:
    return (
        item_id,
        "p1",
        "s1",
        None,
        kind,
        "discovery",
        f"Title {item_id}",
        None,
        None,
        "Some narrative text.",
        "[]",
        "[]",
        "[]",
        "[]",
        "{}",
        1782832648,
        1782832648,
    )


def test_an_unrecognised_kind_is_dropped_not_raised(tmp_path):
    db = _db(
        tmp_path,
        [_row("m1", "observation"), _row("m2", "from-a-future-schema")],
    )

    records = read(db).records

    assert [r.source_id for r in records] == ["m1"]


def test_dropping_one_bad_row_does_not_cost_the_good_rows_around_it(tmp_path):
    db = _db(
        tmp_path,
        [
            _row("m1", "observation"),
            _row("m2", "from-a-future-schema"),
            _row("m3", "summary"),
        ],
    )

    records = read(db).records

    assert [r.source_id for r in records] == ["m1", "m3"]
    assert records[1].kind is SourceKind.SUMMARY


def test_the_dropped_row_is_named_in_skipped_not_merely_dropped(tmp_path):
    """Dropping a row silently leaves nobody able to say afterward that
    anything was lost - `skipped` is what a later task reports as "N rows
    skipped" without re-reading the source database to diff totals."""
    db = _db(
        tmp_path,
        [_row("m1", "observation"), _row("m2", "from-a-future-schema")],
    )

    result = read(db)

    assert len(result.skipped) == 1
    assert "m2" in result.skipped[0]
    assert "from-a-future-schema" in result.skipped[0]
    assert "memory_items" in result.skipped[0]
