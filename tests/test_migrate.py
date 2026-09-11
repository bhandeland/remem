from typing import Any

import psycopg
import pytest

from remem.backends.postgres.migrate import (
    applied_versions,
    migrate,
    pending_versions,
)
from tests.conftest import one

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
