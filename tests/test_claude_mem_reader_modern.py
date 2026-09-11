"""The modern (schema 33+) `memory_items` shape must implement the same
mapping decisions as the legacy reader - the spec's rules are schema-neutral,
not legacy-only. Before this test file existed, `kind='prompt'`,
`kind='summary'` and `kind='manual'` were never read from a modern database
anywhere in the suite: `test_claude_mem_reader.py` only ever inserted a
`kind='observation'` row, and the one modern `kind='summary'` row in
`test_claude_mem_reader_unknown_kind.py` exists to prove a *neighbouring*
row was skipped, not to assert anything about how it is mapped.
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


def _prompt_row(item_id: str, session: str, prompt_text: str, epoch: int) -> tuple:
    return (
        item_id,
        "p1",
        session,
        None,
        "prompt",
        "prompt",
        None,
        None,
        prompt_text,
        None,
        "[]",
        "[]",
        "[]",
        "[]",
        "{}",
        epoch,
        epoch,
    )


def _by_kind(records, kind):
    return [r for r in records if r.kind is kind]


def test_modern_prompts_are_grouped_into_one_record_per_session(tmp_path):
    """The spec rejects one entry per prompt in terms that are not schema
    specific ("seventeen near-empty entries would compete in search results
    against real memories forever") - the modern reader must obey the same
    rule as the legacy one, grouping on `server_session_id` rather than
    `content_session_id`."""
    db = _db(
        tmp_path,
        [
            _prompt_row("m1", "sess-a", "/claude-mem:learn-codebase", 1782832648),
            _prompt_row("m2", "sess-a", "now summarise it", 1782832649),
            _prompt_row(
                "m3", "sess-b", "a prompt from a different session", 1782832650
            ),
        ],
    )

    prompts = _by_kind(read(db).records, SourceKind.PROMPT)

    assert len(prompts) == 2
    by_session = {r.source_id: r for r in prompts}
    assert "prompts:sess-a" in by_session
    assert "prompts:sess-b" in by_session
    sess_a = by_session["prompts:sess-a"]
    assert "/claude-mem:learn-codebase" in sess_a.body
    assert "now summarise it" in sess_a.body


def test_modern_prompts_never_become_one_entry_each(tmp_path):
    """Pinning the defect directly: reading two prompts from the same
    session through `_record` with no grouping would produce two records,
    not one."""
    db = _db(
        tmp_path,
        [
            _prompt_row("m1", "sess-a", "first prompt", 1782832648),
            _prompt_row("m2", "sess-a", "second prompt", 1782832649),
        ],
    )

    prompts = _by_kind(read(db).records, SourceKind.PROMPT)

    assert len(prompts) == 1


def test_a_modern_summary_row_maps_through_the_generic_rendering(tmp_path):
    """The unified table has no `request`/`investigated`/`learned`/
    `completed`/`next_steps` columns - `_summary`'s five-field rendering has
    nothing to read from a modern row. `_record`'s narrative/text/facts/
    concepts rendering is already schema-neutral prose, so a modern summary
    goes through it unchanged rather than through `_summary`, which stays
    reachable only from the legacy reader."""
    row = (
        "m1",
        "p1",
        "sess-a",
        None,
        "summary",
        "summary",
        "Session summary sess-a",
        "A one-line hook",
        None,
        "The session investigated X and learned Y.",
        "[]",
        "[]",
        "[]",
        "[]",
        "{}",
        1782832648,
        1782832648,
    )
    db = _db(tmp_path, [row])

    [record] = read(db).records

    assert record.kind is SourceKind.SUMMARY
    assert record.title == "Session summary sess-a"
    assert record.summary == "A one-line hook"
    assert "investigated X and learned Y" in record.body


def test_a_modern_manual_row_maps_through_the_generic_rendering(tmp_path):
    row = (
        "m1",
        "p1",
        "sess-a",
        None,
        "manual",
        "note",
        "A manually written note",
        None,
        None,
        "Written by hand, not by the extractor.",
        "[]",
        "[]",
        "[]",
        "[]",
        "{}",
        1782832648,
        1782832648,
    )
    db = _db(tmp_path, [row])

    [record] = read(db).records

    assert record.kind is SourceKind.MANUAL
    assert record.title == "A manually written note"
    assert "Written by hand" in record.body


def test_a_migrated_in_place_database_names_its_unread_legacy_tables(tmp_path):
    """`memory_items.legacy_observation_id` is direct evidence that a v33
    database can be one migrated in place from the pre-33 shape - claude-mem's
    own migration is not guaranteed to have dropped the old tables. Reading
    both risks double-importing rows the migration already copied across, so
    the modern reader must name them in `skipped` rather than drop them with
    nothing said."""
    db = _db(tmp_path, [_prompt_row("m1", "sess-a", "hi", 1782832648)])
    conn = sqlite3.connect(db)
    conn.executescript(
        "create table observations (id integer primary key, title text);"
        "create table session_summaries (id integer primary key, request text);"
    )
    conn.execute("insert into observations (id, title) values (1, 'old row')")
    conn.execute("insert into observations (id, title) values (2, 'another old row')")
    conn.commit()
    conn.close()

    result = read(db)

    skipped_text = "\n".join(result.skipped)
    assert "observations" in skipped_text
    assert "2" in skipped_text
    # session_summaries exists but is empty - it must still be named, not
    # silently treated as absent, because "present with zero rows" and
    # "never created" are different facts about the source database.
    assert "session_summaries" in skipped_text
    assert "user_prompts" not in skipped_text
