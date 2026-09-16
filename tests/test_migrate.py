import uuid
from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import (
    applied_versions,
    migrate,
    pending_versions,
)
from tests.conftest import one, scalar

pytestmark = pytest.mark.db


def test_migrate_applies_all_pending(conn: psycopg.Connection[Any]) -> None:
    applied = migrate(conn)
    assert "001_initial" in applied
    assert pending_versions(conn) == []


def test_migrate_is_idempotent(conn: psycopg.Connection[Any]) -> None:
    migrate(conn)
    second = migrate(conn)
    assert second == []


def test_migrate_records_versions(conn: psycopg.Connection[Any]) -> None:
    migrate(conn)
    assert "001_initial" in applied_versions(conn)


def test_schema_has_the_expected_tables(conn: psycopg.Connection[Any]) -> None:
    migrate(conn)
    rows = conn.execute(
        "select table_name from information_schema.tables where table_schema = 'public'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert {"principals", "entries", "collections", "collection_members"} <= names


def test_generated_search_column_is_populated(conn: psycopg.Connection[Any]) -> None:
    migrate(conn)
    conn.execute(
        "insert into principals (id, handle) values "
        "('00000000-0000-7000-8000-000000000001', 'tester')"
    )
    conn.execute(
        "insert into entries (id, kind, title, body, owner_id) values "
        "('00000000-0000-7000-8000-000000000002', 'note', 'Postgres tuning',"
        " 'raise work_mem for big sorts',"
        " '00000000-0000-7000-8000-000000000001')"
    )
    row = one(
        conn.execute(
            "select count(*) from entries "
            "where search @@ websearch_to_tsquery('english', 'work_mem')"
        )
    )
    assert row[0] == 1


def test_transcript_tables_exist_after_migrate(conn: psycopg.Connection[Any]) -> None:
    migrate(conn)
    for table in (
        "transcripts",
        "transcript_lines",
        "transcript_paths",
        "transcript_runs",
    ):
        assert scalar(conn.execute(f"select to_regclass('public.{table}')")) is not None


def test_transcript_lines_cascade_when_their_transcript_goes(
    conn: psycopg.Connection[Any],
) -> None:
    """The derived table must never outlive its source.

    Nothing may store anything only in transcript_lines, and the cascade is
    what makes that enforceable rather than merely intended.
    """
    migrate(conn)
    owner = uuid.uuid4()
    conn.execute(
        "insert into principals (id, handle) values (%s, 'test')",
        (owner,),
    )
    tid = uuid.uuid4()
    conn.execute(
        "insert into transcripts"
        " (id, owner_id, project, harness, session_id, path, content, bytes, sha256)"
        " values (%s, %s, 'p', 'claude-code', 's', '/tmp/s.jsonl', %s, 2, 'abc')",
        (tid, owner, b"{}"),
    )
    conn.execute(
        "insert into transcript_lines (transcript_id, seq, raw)"
        " values (%s, 0, '{}'::jsonb)",
        (tid,),
    )
    conn.execute("delete from transcripts where id = %s", (tid,))
    assert scalar(conn.execute("select count(*) from transcript_lines")) == 0
