"""The retired capture spool.

Migration 010 renames `capture_jobs` rather than dropping it: a pending job
names a transcript the user may still want something from, and deleting that
silently is the one thing the migration must not do. What is asserted here is
that the table survived the rename intact, that nothing reads it any more,
and that the one query left - the pending count - still finds it.
"""

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import new_id

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def test_the_spool_is_renamed_not_dropped(conn):
    migrate(conn)
    names = {
        r[0] for r in conn.execute(
            "select table_name from information_schema.tables "
            "where table_schema = 'public'"
        ).fetchall()
    }
    assert "capture_jobs_legacy" in names
    assert "capture_jobs" not in names


def test_the_renamed_index_follows_the_table(conn):
    migrate(conn)
    row = conn.execute(
        "select indexdef from pg_indexes where indexname = %s",
        ("capture_jobs_legacy_pending_idx",),
    ).fetchone()
    assert row is not None
    definition = row[0].lower()
    assert "capture_jobs_legacy" in definition
    assert "status = \'pending\'" in definition


def test_a_fresh_install_has_nothing_pending(store, owner):
    assert store.pending_legacy_capture_jobs(owner.id) == 0


def test_a_left_over_job_is_counted_for_its_owner_only(conn, store, owner):
    other = store.ensure_principal("someone-else")
    for principal in (owner, other):
        conn.execute(
            "insert into capture_jobs_legacy (id, owner_id, project, "
            "transcript_path) values (%s, %s, %s, %s)",
            (new_id(), principal.id, "remem", "/tmp/t.jsonl"),
        )

    assert store.pending_legacy_capture_jobs(owner.id) == 1


def test_a_finished_job_is_not_pending(conn, store, owner):
    job_id = new_id()
    conn.execute(
        "insert into capture_jobs_legacy (id, owner_id, project, "
        "transcript_path, status) values (%s, %s, %s, %s, \'done\')",
        (job_id, owner.id, "remem", "/tmp/t.jsonl"),
    )
    assert store.pending_legacy_capture_jobs(owner.id) == 0
