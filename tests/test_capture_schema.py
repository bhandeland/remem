import pytest

from remem.backends.postgres.migrate import migrate
from remem.domain import CaptureJob, CaptureStatus, Origin, Query, new_id

pytestmark = pytest.mark.db


def test_capture_status_values():
    assert CaptureStatus.PENDING == "pending"
    assert CaptureStatus.RUNNING == "running"
    assert CaptureStatus.DONE == "done"
    assert CaptureStatus.FAILED == "failed"


def test_capture_job_defaults():
    job = CaptureJob(
        id=new_id(),
        owner_id=new_id(),
        project="remem",
        transcript_path="/tmp/t.jsonl",
    )
    assert job.status is CaptureStatus.PENDING
    assert job.attempts == 0
    assert job.error is None
    assert job.entries_written == 0
    assert job.session_id is None


def test_query_origins_defaults_to_empty_meaning_all():
    assert Query().origins == []


def test_query_origins_are_not_shared_between_instances():
    a, b = Query(), Query()
    a.origins.append(Origin.EXTRACTED)
    assert b.origins == []


def test_capture_tables_exist(conn):
    migrate(conn)
    rows = conn.execute(
        "select table_name from information_schema.tables "
        "where table_schema = 'public'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert {"record_settings", "capture_jobs"} <= names


def test_capture_jobs_pending_index_is_partial(conn):
    migrate(conn)
    row = conn.execute(
        "select indexdef from pg_indexes where indexname = %s",
        ("capture_jobs_pending_idx",),
    ).fetchone()
    assert row is not None
    definition = row[0].lower()
    assert "where" in definition
    assert "status = 'pending'" in definition


def test_capture_job_timestamps_use_clock_timestamp(conn):
    migrate(conn)
    row = conn.execute(
        "select column_default from information_schema.columns "
        "where table_name = 'capture_jobs' and column_name = 'created_at'"
    ).fetchone()
    assert "clock_timestamp" in row[0]
