"""The pre-33 claude-mem schema: three tables instead of one.

The column list is copied from a real export (2026-06-30), not from the
current upstream schema - this fixture exists to pin the shape the code has
to survive, and deriving it from today's schema would pin nothing.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from remem.importers.base import SourceKind
from remem.importers.claude_mem import read

LEGACY_SCHEMA = """
create table observations (
  id integer primary key, memory_session_id text, project text,
  text text, type text, title text, subtitle text,
  facts text, concepts text, files_read text, files_modified text,
  narrative text, metadata text, created_at text,
  created_at_epoch integer, prompt_number integer,
  agent_id text, agent_type text, content_hash text,
  discovery_tokens integer, generated_by_model text,
  merged_into_project text, relevance_count integer
);
create table session_summaries (
  id integer primary key, memory_session_id text, project text,
  request text, investigated text, learned text, completed text,
  next_steps text, notes text, files_read text, files_edited text,
  created_at text, created_at_epoch integer, prompt_number integer,
  discovery_tokens integer, merged_into_project text
);
create table user_prompts (
  id integer primary key, session_db_id integer, content_session_id text,
  prompt_number integer, prompt_text text,
  created_at text, created_at_epoch integer
);
"""


def _legacy(tmp_path: Path) -> Path:
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "insert into observations (id, memory_session_id, project, type, title,"
        " subtitle, facts, concepts, narrative, created_at_epoch)"
        " values (?,?,?,?,?,?,?,?,?,?)",
        (
            1,
            "sess-a",
            "at-workspace",
            "discovery",
            "Workspace technology inventory",
            "Mapped 80+ projects",
            json.dumps(["Workspace occupies 75GB"]),
            json.dumps(["how-it-works: git repos are the unit"]),
            "The user ran a comprehensive scan.",
            1782832648000,
        ),
    )
    conn.execute(
        "insert into session_summaries (id, memory_session_id, project, request,"
        " investigated, learned, completed, next_steps, created_at_epoch)"
        " values (?,?,?,?,?,?,?,?,?)",
        (
            1,
            "sess-a",
            "at-workspace",
            "User invoked learn-codebase",
            "Scanned all 75+ directories",
            "Workspace is a meta-workspace",
            "Created a topology reference",
            "Session concluded",
            1782832649000,
        ),
    )
    for n, text in ((1, "/claude-mem:learn-codebase"), (2, "now summarise it")):
        conn.execute(
            "insert into user_prompts (id, session_db_id, content_session_id,"
            " prompt_number, prompt_text, created_at_epoch) values (?,?,?,?,?,?)",
            (n, 1, "sess-a", n, text, 1782832650000 + n),
        )
    conn.commit()
    conn.close()
    return db


def _by_kind(records, kind):
    return [r for r in records if r.kind is kind]


def test_all_three_legacy_tables_are_read(tmp_path):
    records = read(_legacy(tmp_path)).records

    assert len(_by_kind(records, SourceKind.OBSERVATION)) == 1
    assert len(_by_kind(records, SourceKind.SUMMARY)) == 1
    assert len(_by_kind(records, SourceKind.PROMPT)) == 1


def test_a_legacy_observation_maps_like_a_modern_one(tmp_path):
    [obs] = _by_kind(read(_legacy(tmp_path)).records, SourceKind.OBSERVATION)

    assert obs.source_id == "1"
    assert obs.summary == "Mapped 80+ projects"
    assert obs.tags == ("cmem-type:discovery",)
    assert "Workspace occupies 75GB" in obs.body


def test_a_summary_renders_its_five_fields(tmp_path):
    [summary] = _by_kind(read(_legacy(tmp_path)).records, SourceKind.SUMMARY)

    for expected in (
        "User invoked learn-codebase",
        "Scanned all 75+ directories",
        "Workspace is a meta-workspace",
        "Created a topology reference",
        "Session concluded",
    ):
        assert expected in summary.body


def test_prompts_are_grouped_into_one_record_per_session(tmp_path):
    """One entry per prompt would be near-empty entries competing in search
    against real memories. The sequence is the signal, not the string."""
    [prompts] = _by_kind(read(_legacy(tmp_path)).records, SourceKind.PROMPT)

    assert prompts.source_id == "prompts:sess-a"
    assert "/claude-mem:learn-codebase" in prompts.body
    assert "now summarise it" in prompts.body


def test_milliseconds_are_not_read_as_seconds(tmp_path):
    """Legacy epochs are milliseconds. Read as seconds they land in 1970."""
    [obs] = _by_kind(read(_legacy(tmp_path)).records, SourceKind.OBSERVATION)

    assert obs.created_at is not None
    assert obs.created_at.year == 2026
