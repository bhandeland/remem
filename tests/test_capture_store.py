import psycopg
import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import CaptureJob, CaptureStatus, new_id

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def _job(owner_id, project="remem", path="/tmp/t.jsonl"):
    return CaptureJob(id=new_id(), owner_id=owner_id, project=project,
                      transcript_path=path, session_id="sess-1")


def test_capture_is_disabled_by_default(store, owner):
    assert store.capture_enabled(owner.id, "remem") is False


def test_enable_then_disable(store, owner):
    store.set_capture_enabled(owner.id, "remem", True)
    assert store.capture_enabled(owner.id, "remem") is True
    store.set_capture_enabled(owner.id, "remem", False)
    assert store.capture_enabled(owner.id, "remem") is False


def test_enabling_twice_does_not_error(store, owner):
    store.set_capture_enabled(owner.id, "remem", True)
    store.set_capture_enabled(owner.id, "remem", True)
    assert store.capture_enabled(owner.id, "remem") is True


def test_settings_are_owner_scoped(store, owner):
    other = store.ensure_principal("someone-else")
    store.set_capture_enabled(owner.id, "remem", True)
    assert store.capture_enabled(other.id, "remem") is False


def test_enqueue_then_claim(store, owner):
    job = store.enqueue_capture(_job(owner.id))
    claimed = store.claim_capture_jobs(owner.id, limit=10)
    assert [c.id for c in claimed] == [job.id]
    assert claimed[0].status is CaptureStatus.RUNNING
    assert claimed[0].attempts == 1
    assert claimed[0].transcript_path == "/tmp/t.jsonl"
    assert claimed[0].session_id == "sess-1"


def test_claiming_twice_yields_nothing_the_second_time(store, owner):
    store.enqueue_capture(_job(owner.id))
    assert len(store.claim_capture_jobs(owner.id, limit=10)) == 1
    assert store.claim_capture_jobs(owner.id, limit=10) == []


def test_claim_respects_the_limit(store, owner):
    for i in range(5):
        store.enqueue_capture(_job(owner.id, path=f"/tmp/{i}.jsonl"))
    assert len(store.claim_capture_jobs(owner.id, limit=2)) == 2


def test_claim_is_owner_scoped(store, owner):
    other = store.ensure_principal("someone-else")
    store.enqueue_capture(_job(other.id))
    assert store.claim_capture_jobs(owner.id, limit=10) == []


def test_finish_marks_done_and_records_the_count(store, owner):
    job = store.enqueue_capture(_job(owner.id))
    store.claim_capture_jobs(owner.id, limit=1)
    store.finish_capture_job(job.id, owner.id, CaptureStatus.DONE, None, 3)
    stored = store.get_capture_job(job.id, owner.id)
    assert stored.status is CaptureStatus.DONE
    assert stored.entries_written == 3
    assert stored.error is None


def test_finish_records_a_failure_reason(store, owner):
    job = store.enqueue_capture(_job(owner.id))
    store.claim_capture_jobs(owner.id, limit=1)
    store.finish_capture_job(job.id, owner.id, CaptureStatus.FAILED,
                             "claude timed out", 0)
    stored = store.get_capture_job(job.id, owner.id)
    assert stored.status is CaptureStatus.FAILED
    assert stored.error == "claude timed out"


def test_counts_report_each_status(store, owner):
    a = store.enqueue_capture(_job(owner.id, path="/tmp/a.jsonl"))
    store.enqueue_capture(_job(owner.id, path="/tmp/b.jsonl"))
    store.claim_capture_jobs(owner.id, limit=1)
    store.finish_capture_job(a.id, owner.id, CaptureStatus.DONE, None, 1)
    counts = store.capture_job_counts(owner.id)
    assert counts.get("done") == 1
    assert counts.get("pending") == 1


def test_recent_failures_are_listed_newest_first(store, owner):
    for i in range(2):
        job = store.enqueue_capture(_job(owner.id, path=f"/tmp/{i}.jsonl"))
        store.claim_capture_jobs(owner.id, limit=1)
        store.finish_capture_job(job.id, owner.id, CaptureStatus.FAILED,
                                 f"reason {i}", 0)
    failures = store.recent_failed_capture_jobs(owner.id)
    assert [f.error for f in failures] == ["reason 1", "reason 0"]


def test_a_stale_running_job_is_reclaimed(store, owner, conn):
    """A drain killed mid-job must not strand work nothing retries."""
    job = store.enqueue_capture(_job(owner.id))
    store.claim_capture_jobs(owner.id, limit=1)
    conn.execute(
        "update capture_jobs set updated_at = clock_timestamp() - interval '1 hour' "
        "where id = %s",
        (job.id,),
    )
    reclaimed = store.claim_capture_jobs(owner.id, limit=1, stale_after_seconds=600)
    assert [r.id for r in reclaimed] == [job.id]
    assert reclaimed[0].attempts == 2


def test_a_fresh_running_job_is_not_reclaimed(store, owner):
    store.enqueue_capture(_job(owner.id))
    store.claim_capture_jobs(owner.id, limit=1)
    assert store.claim_capture_jobs(owner.id, limit=1, stale_after_seconds=600) == []


@pytest.mark.db
def test_two_concurrent_drains_do_not_claim_the_same_job(live_dsn):
    """SKIP LOCKED is what makes a second drain safe."""
    with psycopg.connect(live_dsn) as setup:
        migrate(setup)
        setup.commit()
        store = PostgresStore(setup)
        owner = store.ensure_principal("brandon")
        store.enqueue_capture(_job(owner.id))
        setup.commit()

    with psycopg.connect(live_dsn) as a, psycopg.connect(live_dsn) as b:
        claimed_a = PostgresStore(a).claim_capture_jobs(owner.id, limit=10)
        claimed_b = PostgresStore(b).claim_capture_jobs(owner.id, limit=10)
        a.commit()
        b.commit()

    assert len(claimed_a) + len(claimed_b) == 1


def test_enabled_capture_projects_lists_only_enabled_ones_for_this_owner(store, owner):
    other = store.ensure_principal("someone-else")
    store.set_capture_enabled(owner.id, "beta", True)
    store.set_capture_enabled(owner.id, "alpha", True)
    store.set_capture_enabled(owner.id, "gamma", False)
    store.set_capture_enabled(other.id, "theirs", True)

    assert store.enabled_capture_projects(owner.id) == ["alpha", "beta"]
