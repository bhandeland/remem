"""The claude-mem sqlite reader. Pure - builds its own database, no store.

Column names and values are spelled literally, never imported from the
module under test: this is an external contract, and a test that renames
itself alongside the code pins nothing.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from remem.importers.base import SourceKind
from remem.importers.claude_mem import UnreadableSource, read

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


def _modern(tmp_path: Path) -> Path:
    db = tmp_path / "claude-mem.db"
    conn = sqlite3.connect(db)
    conn.executescript(MODERN_SCHEMA)
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
            "Workspace technology inventory",
            "Mapped 80+ projects across Python, Node and Terraform",
            None,
            "The user ran a comprehensive scan of their workspace.",
            json.dumps(["Workspace occupies 75GB", "80+ project directories"]),
            json.dumps(["how-it-works: git repos are the unit"]),
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


def test_an_observation_becomes_one_record(tmp_path):
    [record] = read(_modern(tmp_path)).records

    assert record.source_id == "m1"
    assert record.kind is SourceKind.OBSERVATION
    assert record.project == "at-workspace"
    assert record.title == "Workspace technology inventory"


def test_the_subtitle_becomes_the_summary(tmp_path):
    """claude-mem's subtitle is already a one-line hook written to sit under
    a title, which is exactly what remem's summary field is for."""
    [record] = read(_modern(tmp_path)).records

    assert record.summary == "Mapped 80+ projects across Python, Node and Terraform"


def test_facts_and_concepts_are_rendered_into_the_body(tmp_path):
    """Facts and concepts render as prose (bullet lists), not JSON.

    The body is half the embedding text and the bulk of the tsvector, so a
    serialised array here would be searched as punctuation, breaking recall.
    """
    [record] = read(_modern(tmp_path)).records

    # Prose substring containment (necessary but not sufficient)
    assert "comprehensive scan" in record.body

    # Shape assertion: facts render as a bullet list, not JSON
    assert "## Facts\n\n- Workspace occupies 75GB" in record.body
    assert "- 80+ project directories" in record.body

    # Concepts also render as bullet list
    assert "## Concepts\n\n- how-it-works: git repos are the unit" in record.body


def test_facts_never_become_tags(tmp_path):
    """Tags are the highest-weighted tsvector field after the title, so a
    sentence in a tag distorts ranking for every query sharing a word."""
    [record] = read(_modern(tmp_path)).records

    assert record.tags == ("cmem-type:discovery",)


def test_a_file_that_is_not_a_database_is_refused_by_name(tmp_path):
    junk = tmp_path / "notes.txt"
    junk.write_text("this is not sqlite")

    with pytest.raises(UnreadableSource, match="notes.txt"):
        read(junk)


def test_a_database_with_no_recognised_tables_names_what_it_looked_for(tmp_path):
    db = tmp_path / "other.db"
    conn = sqlite3.connect(db)
    conn.executescript("create table unrelated (id integer primary key);")
    conn.commit()
    conn.close()

    with pytest.raises(UnreadableSource) as exc:
        read(db)

    assert "memory_items" in str(exc.value)
    assert "observations" in str(exc.value)
