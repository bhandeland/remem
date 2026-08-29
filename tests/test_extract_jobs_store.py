"""The extraction spool: the idle rule, the covers_through watermark, and
claim/finish mechanics that `remem events process` (Task 6) will drive."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Event, EventKind, JobStatus, new_id

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def put_event(store, owner, *, at, session="s1", project="remem",
              harness="claude-code"):
    """A minimal tool_call event, timestamped by the caller.

    The watermark tests care only about `occurred_at`; everything else is
    filler that satisfies the NOT NULL columns.
    """
    return store.put_event(
        Event(
            id=new_id(),
            owner_id=owner.id,
            project=project,
            harness=harness,
            session_id=session,
            kind=EventKind.TOOL_CALL,
            tool="Bash",
            payload={"command": "ls"},
            occurred_at=at,
        )
    )


def test_a_session_with_recent_events_is_not_awaiting_extraction(store, owner):
    """The idle rule, from the store's side.

    A session that is still being worked in has events arriving; extracting
    it now means extracting half a session and then having to decide what to
    do with the other half.
    """
    put_event(store, owner, at=now_utc())
    assert store.sessions_awaiting_extraction(owner.id, idle_seconds=1200,
                                              limit=10) == []


def test_a_quiet_session_is_awaiting_extraction(store, owner):
    put_event(store, owner, at=now_utc() - timedelta(hours=2))
    [session] = store.sessions_awaiting_extraction(owner.id, 1200, 10)
    assert session.session_id == "s1"
    assert session.event_count == 1
    assert session.extract_from is None


def test_a_resumed_session_comes_back_with_a_watermark(store, owner):
    """The reason covers_through exists.

    "This session has a done job" is the obvious definition of extracted and
    it is wrong: a session that gets resumed records new events after that
    job, and under the obvious rule those events are born already-extracted -
    invisible to process, and eligible for prune having produced nothing.
    """
    first = now_utc() - timedelta(hours=3)
    put_event(store, owner, at=first)
    session = store.sessions_awaiting_extraction(owner.id, 1200, 10)[0]
    job = store.claim_extract_job(owner.id, session)
    store.finish_extract_job(job.id, owner.id, JobStatus.DONE, None, 2,
                             covers_through=first)

    assert store.sessions_awaiting_extraction(owner.id, 1200, 10) == []

    resumed_at = now_utc() - timedelta(hours=1)
    put_event(store, owner, at=resumed_at)

    [again] = store.sessions_awaiting_extraction(owner.id, 1200, 10)
    assert again.extract_from == first
    assert again.event_count == 1  # only the unextracted one


def test_claiming_twice_reuses_the_row_and_counts_attempts(store, owner):
    put_event(store, owner, at=now_utc() - timedelta(hours=2))
    session = store.sessions_awaiting_extraction(owner.id, 1200, 10)[0]

    first = store.claim_extract_job(owner.id, session)
    second = store.claim_extract_job(owner.id, session)

    assert second.id == first.id
    assert second.attempts == 2
    assert second.status == JobStatus.RUNNING

    got = store.get_extract_job(first.id, owner.id)
    assert got is not None
    assert got.attempts == 2


def test_get_extract_job_returns_none_when_absent(store, owner):
    assert store.get_extract_job(new_id(), owner.id) is None


def test_claim_extract_job_by_id_reclaims_a_named_job(store, owner):
    put_event(store, owner, at=now_utc() - timedelta(hours=2))
    session = store.sessions_awaiting_extraction(owner.id, 1200, 10)[0]
    job = store.claim_extract_job(owner.id, session)
    store.finish_extract_job(job.id, owner.id, JobStatus.FAILED,
                             "boom", 0, covers_through=None)

    reclaimed = store.claim_extract_job_by_id(job.id, owner.id)
    assert reclaimed is not None
    assert reclaimed.id == job.id
    assert reclaimed.attempts == 2
    assert reclaimed.status == JobStatus.RUNNING


def test_claim_extract_job_by_id_returns_none_for_another_owner(store, owner):
    put_event(store, owner, at=now_utc() - timedelta(hours=2))
    session = store.sessions_awaiting_extraction(owner.id, 1200, 10)[0]
    job = store.claim_extract_job(owner.id, session)

    other = store.ensure_principal("someone-else")
    assert store.claim_extract_job_by_id(job.id, other.id) is None


def test_extract_job_counts_groups_by_status(store, owner):
    put_event(store, owner, at=now_utc() - timedelta(hours=2), session="a")
    put_event(store, owner, at=now_utc() - timedelta(hours=2), session="b")
    sessions = store.sessions_awaiting_extraction(owner.id, 1200, 10)
    jobs = [store.claim_extract_job(owner.id, s) for s in sessions]
    store.finish_extract_job(jobs[0].id, owner.id, JobStatus.DONE, None, 1,
                             covers_through=now_utc())
    store.finish_extract_job(jobs[1].id, owner.id, JobStatus.FAILED, "boom",
                             0, covers_through=None)

    counts = store.extract_job_counts(owner.id)
    assert counts["done"] == 1
    assert counts["failed"] == 1


def test_recent_failed_extract_jobs_orders_newest_first(store, owner):
    put_event(store, owner, at=now_utc() - timedelta(hours=2), session="a")
    put_event(store, owner, at=now_utc() - timedelta(hours=2), session="b")
    sessions = store.sessions_awaiting_extraction(owner.id, 1200, 10)
    jobs = [store.claim_extract_job(owner.id, s) for s in sessions]
    store.finish_extract_job(jobs[0].id, owner.id, JobStatus.FAILED,
                             "first failure", 0, covers_through=None)
    store.finish_extract_job(jobs[1].id, owner.id, JobStatus.FAILED,
                             "second failure", 0, covers_through=None)

    [newest, oldest] = store.recent_failed_extract_jobs(owner.id, limit=5)
    assert newest.error == "second failure"
    assert oldest.error == "first failure"


def test_the_advisory_lock_is_per_command_and_owner(store, owner):
    """Two `events process` runs must not process the same session; a
    process run and an embed run must not block each other."""
    assert store.try_advisory_lock("events-process", owner.id) is True
    other = store.ensure_principal("someone-else")
    assert store.try_advisory_lock("events-process", other.id) is True
    assert store.try_advisory_lock("embed", owner.id) is True
