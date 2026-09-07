"""018 creates memory_runs with the shape the service writes."""

from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate

pytestmark = pytest.mark.db


def _columns(conn, table):
    rows = conn.execute(
        "select column_name, is_nullable, data_type "
        "from information_schema.columns where table_name = %s",
        (table,),
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def test_memory_runs_exists_with_its_counts(conn):
    migrate(conn)
    cols = _columns(conn, "memory_runs")
    for name in ("adopted", "healed", "edited", "regenerated", "deleted", "unchanged"):
        assert cols[name][0] == "NO", name
    for name in ("renamed", "conflicts", "sidecars", "failures"):
        assert cols[name][1] == "jsonb", name
    # finished_at nullable is the whole crash-detection design.
    assert cols["finished_at"][0] == "YES"
    assert cols["started_at"][0] == "NO"


def test_the_trigger_check_rejects_an_unknown_value(conn):
    import psycopg

    migrate(conn)
    conn.execute(
        "insert into principals (id, handle) values "
        "('00000000-0000-0000-0000-000000000001', 'trigger-check')"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "insert into memory_runs (id, owner_id, project, trigger) values "
            "('00000000-0000-0000-0000-000000000002',"
            " '00000000-0000-0000-0000-000000000001', 'p', 'sideways')"
        )
