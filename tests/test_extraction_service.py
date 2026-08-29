"""Extraction: events in, entries and provenance out.

The round trip is the point. Capture's tests could stop at "an entry was
written"; extraction cannot, because the entry is only half of what a run
produces - the other half is the `entry_events` rows that say which raw
events it came out of, and an entry with no provenance is exactly the
unauditable thing this pipeline exists to replace.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from remem.agents.base import HarnessEvent
from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import EventKind, JobStatus, Kind, Origin, Query, new_id
from remem.extract.base import ExtractedEntry, ExtractionFailed, parse_entries
from remem.services import extraction, record

pytestmark = pytest.mark.db

NOW = datetime.now(timezone.utc)
IDLE = 1200


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def a_harness_event(**kw):
    return HarnessEvent(
        kind=kw.get("kind", EventKind.TOOL_CALL),
        session_id=kw.get("session_id", "s1"),
        project=kw.get("project", "remem"),
        tool=kw.get("tool", "Bash"),
        payload=kw.get("payload", {"command": "ls"}),
        occurred_at=kw.get("occurred_at", NOW - timedelta(hours=2)),
    )


def three_events(store, owner, session_id="s1", project="remem", base=None):
    """Three events, spaced, all old enough for the idle trigger."""
    base = base or (NOW - timedelta(hours=2))
    return [
        record.record(
            store,
            owner.id,
            a_harness_event(
                session_id=session_id,
                project=project,
                occurred_at=base + timedelta(seconds=i),
                payload={"command": f"ls {i}"},
            ),
            "claude-code",
        )
        for i in range(3)
    ]


class FakeExtractor:
    """Records what it was given, returns what it was told to.

    A fake rather than a stub because the interesting assertion is about the
    INPUT: the extractor must be handed events, and never a transcript path.
    """

    def __init__(self, entries=None, error=None):
        self.entries = entries or []
        self.error = error
        self.seen: list[list] = []
        self.known: list[list[str]] = []

    def extract(self, events, project, known_titles=None):
        self.seen.append(list(events))
        self.known.append(list(known_titles or []))
        if self.error:
            raise self.error
        return list(self.entries)


def an_entry(title="pgvector needs no index yet", body="Under 10k rows a "
             "sequential scan beats an ivfflat index."):
    return ExtractedEntry(title=title, body=body, kind=Kind.NOTE,
                          tags=["postgres"])


# --- the round trip ---------------------------------------------------------


def test_processing_a_quiet_session_writes_entries_and_provenance(store, owner):
    record.enable(store, owner.id, "remem")
    events = three_events(store, owner)
    extractor = FakeExtractor([an_entry()])

    report = extraction.process(store, owner.id, extractor,
                                idle_seconds=IDLE, limit=10)

    assert (report.claimed, report.succeeded, report.entries_written) == (1, 1, 1)
    assert report.failed == 0
    [written] = store.search(
        Query(project="remem", origins=[Origin.EXTRACTED], limit=10), owner.id
    )
    assert written.entry.origin is Origin.EXTRACTED
    assert written.entry.session_id == "s1"
    assert written.entry.title == "pgvector needs no index yet"
    assert written.entry.tags == ["postgres"]
    assert written.entry.agent == "claude-code"
    rows = store.provenance(written.entry.id, owner.id)
    assert {r[0] for r in rows} == {e.id for e in events}
    assert all(r[1:] == ("s1", "claude-code", True) for r in rows)


def test_the_extractor_is_handed_events_not_a_transcript(store, owner):
    """The input is rows, oldest first - nothing here reads a file."""
    record.enable(store, owner.id, "remem")
    events = three_events(store, owner)
    extractor = FakeExtractor([])

    extraction.process(store, owner.id, extractor, idle_seconds=IDLE, limit=10)

    assert len(extractor.seen) == 1
    assert [e.id for e in extractor.seen[0]] == [e.id for e in events]
    assert [e.payload for e in extractor.seen[0]] == [
        {"command": "ls 0"}, {"command": "ls 1"}, {"command": "ls 2"}
    ]


def test_a_second_run_extracts_nothing_new(store, owner):
    """Idempotence. `events process` is a cron command; running it twice
    must do the work once."""
    record.enable(store, owner.id, "remem")
    three_events(store, owner)
    extractor = FakeExtractor([an_entry()])

    first = extraction.process(store, owner.id, extractor,
                               idle_seconds=IDLE, limit=10)
    second = extraction.process(store, owner.id, extractor,
                                idle_seconds=IDLE, limit=10)

    assert first.claimed == 1
    assert (second.claimed, second.succeeded, second.failed,
            second.entries_written) == (0, 0, 0, 0)
    assert len(extractor.seen) == 1


def test_a_session_still_being_worked_in_is_left_alone(store, owner):
    """The idle window is the whole trigger: extracting a live session would
    read half a conversation and then never look again."""
    record.enable(store, owner.id, "remem")
    record.record(
        store, owner.id, a_harness_event(occurred_at=NOW), "claude-code"
    )
    extractor = FakeExtractor([an_entry()])

    report = extraction.process(store, owner.id, extractor,
                                idle_seconds=IDLE, limit=10)

    assert report.claimed == 0
    assert extractor.seen == []


def test_a_resumed_session_extracts_only_its_new_events(store, owner):
    record.enable(store, owner.id, "remem")
    three_events(store, owner)
    extractor = FakeExtractor([])
    extraction.process(store, owner.id, extractor, idle_seconds=IDLE, limit=10)

    later = [
        record.record(
            store,
            owner.id,
            a_harness_event(
                occurred_at=NOW - timedelta(hours=1) + timedelta(seconds=i),
                payload={"command": f"later {i}"},
            ),
            "claude-code",
        )
        for i in range(2)
    ]

    report = extraction.process(store, owner.id, extractor,
                                idle_seconds=IDLE, limit=10)

    assert report.claimed == 1
    assert [e.id for e in extractor.seen[1]] == [e.id for e in later]


def test_an_extractor_that_raises_fails_the_job_and_records_why(store, owner):
    record.enable(store, owner.id, "remem")
    three_events(store, owner)
    prose = "I read the session and found nothing worth remembering."

    class ProseExtractor:
        def extract(self, events, project, known_titles=None):
            return parse_entries(prose)

    report = extraction.process(store, owner.id, ProseExtractor(),
                                idle_seconds=IDLE, limit=10)

    assert (report.claimed, report.succeeded, report.failed) == (1, 0, 1)
    [job] = store.recent_failed_extract_jobs(owner.id)
    assert job.status is JobStatus.FAILED
    assert "no JSON array" in job.error
    assert prose in job.error


def test_an_empty_event_list_is_a_quiet_session_not_a_failure(conn, store, owner):
    """`process_job` on a session whose events have all been extracted
    already: nothing to read is not a broken job."""
    record.enable(store, owner.id, "remem")
    three_events(store, owner)
    extractor = FakeExtractor([])
    extraction.process(store, owner.id, extractor, idle_seconds=IDLE, limit=10)
    [job] = _all_jobs(conn, store, owner)
    job_id = job.id

    report = extraction.process_job(store, owner.id, job_id, extractor)

    assert (report.claimed, report.succeeded, report.failed) == (1, 1, 0)
    assert store.get_extract_job(job_id, owner.id).status is JobStatus.DONE


def _all_jobs(conn, store, owner):
    """Every extract job for this owner, whatever its status.

    The store exposes counts and recent failures but no listing - right for
    the CLI, unhelpful here - so the test reads the table directly rather
    than adding a method nothing in production wants.
    """
    ids = [
        r[0] for r in conn.execute(
            "select id from extract_jobs where owner_id = %s order by created_at",
            (owner.id,),
        ).fetchall()
    ]
    return [store.get_extract_job(i, owner.id) for i in ids]


def test_the_attempt_cap_stops_a_job_and_process_job_overrides_it(
    conn, store, owner
):
    """A job that can never succeed must stop being retried and say so -
    and `--job ID` is the only way back for one that has given up."""
    record.enable(store, owner.id, "remem")
    three_events(store, owner)
    boom = FakeExtractor(error=RuntimeError("claude exploded"))

    for _ in range(extraction.MAX_ATTEMPTS + 2):
        extraction.process(store, owner.id, boom, idle_seconds=IDLE, limit=10)

    [job] = _all_jobs(conn, store, owner)
    assert job.status is JobStatus.FAILED
    assert "gave up" in job.error
    # The extractor stopped being called once the cap was passed.
    assert len(boom.seen) == extraction.MAX_ATTEMPTS

    report = extraction.process_job(store, owner.id, job.id,
                                    FakeExtractor([an_entry()]))


    assert (report.claimed, report.succeeded, report.entries_written) == (1, 1, 1)
    assert store.get_extract_job(job.id, owner.id).status is JobStatus.DONE


def test_a_session_that_gave_up_leaves_the_backlog(conn, store, owner):
    """The failure mode discovery introduces: `sessions_awaiting_extraction`
    computes watermarks from DONE jobs only, so a session whose job failed
    still looks outstanding. Without the skip, every later run reclaims it,
    records "gave up" again, and reports `failed 1` forever - and because
    discovery is oldest-first, dead sessions sort ahead of live ones and
    fill the batch."""
    record.enable(store, owner.id, "remem")
    three_events(store, owner)
    boom = FakeExtractor(error=RuntimeError("claude exploded"))

    for _ in range(extraction.MAX_ATTEMPTS + 1):
        extraction.process(store, owner.id, boom, idle_seconds=IDLE, limit=10)
    [job] = _all_jobs(conn, store, owner)
    assert job.status is JobStatus.FAILED
    attempts_at_giving_up = job.attempts

    report = extraction.process(store, owner.id, boom,
                                idle_seconds=IDLE, limit=10)

    assert (report.claimed, report.failed, report.succeeded) == (0, 0, 0)
    after = store.get_extract_job(job.id, owner.id)
    assert after.attempts == attempts_at_giving_up
    assert after.error == job.error


def test_a_dead_session_does_not_crowd_out_a_live_one(conn, store, owner):
    """Discovery is ordered oldest-event-first, so a session that has given
    up sorts ahead of a newer one. With --limit 1 it would take the whole
    batch, every run, and the newer session would never be extracted."""
    record.enable(store, owner.id, "remem")
    three_events(store, owner, session_id="dead", base=NOW - timedelta(hours=5))
    boom = FakeExtractor(error=RuntimeError("claude exploded"))
    for _ in range(extraction.MAX_ATTEMPTS + 1):
        extraction.process(store, owner.id, boom, idle_seconds=IDLE, limit=1)

    three_events(store, owner, session_id="live", base=NOW - timedelta(hours=2))
    good = FakeExtractor([an_entry()])

    report = extraction.process(store, owner.id, good, idle_seconds=IDLE,
                                limit=1)

    assert (report.claimed, report.succeeded, report.entries_written) == (1, 1, 1)
    assert [e.session_id for e in good.seen[0]] == ["live", "live", "live"]


def test_process_job_rejects_an_unknown_id(store, owner):
    with pytest.raises(extraction.ExtractJobNotFound):
        extraction.process_job(store, owner.id, new_id(), FakeExtractor([]))


def test_process_job_rejects_another_owners_job(conn, store, owner):
    """Owner scoping is not optional just because an id was supplied."""
    other = store.ensure_principal("someone-else")
    record.enable(store, other.id, "remem")
    three_events(store, other)
    extraction.process(store, other.id, FakeExtractor([]),
                       idle_seconds=IDLE, limit=10)
    [job] = _all_jobs(conn, store, other)

    with pytest.raises(extraction.ExtractJobNotFound):
        extraction.process_job(store, owner.id, job.id, FakeExtractor([]))


def test_an_entry_the_project_already_holds_is_not_rewritten(store, owner):
    """Carried over from capture unchanged: only a prior EXTRACTED entry
    with the same title suppresses a write. A human-written entry with that
    title is not a duplicate to swallow silently."""
    from remem.services.write import remember

    record.enable(store, owner.id, "remem")
    remember(store, owner.id, title="Pool sizing", body="the user wrote this",
             project="remem", origin=Origin.HUMAN)
    three_events(store, owner, session_id="a")
    three_events(store, owner, session_id="b",
                 base=NOW - timedelta(hours=3))
    entry = ExtractedEntry(title="Pool sizing", body="extracted", kind=Kind.NOTE)

    report = extraction.process(store, owner.id, FakeExtractor([entry]),
                                idle_seconds=IDLE, limit=10)

    # Two sessions, one entry each - and the second is suppressed by the
    # first, while the human's entry with the same title survives untouched.
    assert report.claimed == 2
    assert report.entries_written == 1
    titles = [h.entry.title for h in store.search(Query(text="Pool sizing"),
                                                  owner.id)]
    assert titles.count("Pool sizing") == 2


def test_dedup_does_not_block_the_same_title_in_another_project(store, owner):
    record.enable(store, owner.id, "alpha")
    record.enable(store, owner.id, "beta")
    three_events(store, owner, project="alpha", session_id="a")
    three_events(store, owner, project="beta", session_id="b",
                 base=NOW - timedelta(hours=3))
    entry = ExtractedEntry(title="Pool sizing", body="b", kind=Kind.NOTE)

    report = extraction.process(store, owner.id, FakeExtractor([entry]),
                                idle_seconds=IDLE, limit=10)

    assert report.entries_written == 2


def test_the_extractor_is_told_what_the_project_already_holds(store, owner):
    """Including entries the USER wrote. The observed failure was extraction
    re-deriving a hand-written rule, so human titles are exactly the ones the
    extractor most needs to see."""
    from remem.services.write import remember

    remember(store, owner.id, title="A rule the user wrote", body="b",
             project="remem", origin=Origin.HUMAN)
    remember(store, owner.id, title="An earlier extraction", body="b",
             project="remem", origin=Origin.EXTRACTED)
    record.enable(store, owner.id, "remem")
    three_events(store, owner)
    extractor = FakeExtractor([])

    extraction.process(store, owner.id, extractor, idle_seconds=IDLE, limit=10)

    assert "A rule the user wrote" in extractor.known[0]
    assert "An earlier extraction" in extractor.known[0]


def test_an_extractor_without_known_titles_support_still_works(store, owner):
    """The protocol's third argument is optional; a two-argument extractor
    must keep working rather than failing with a TypeError recorded as an
    extraction failure."""
    record.enable(store, owner.id, "remem")
    three_events(store, owner)

    class TwoArg:
        def extract(self, events, project):
            return []

    report = extraction.process(store, owner.id, TwoArg(),
                                idle_seconds=IDLE, limit=10)
    assert (report.claimed, report.succeeded) == (1, 1)


def test_a_long_raw_output_is_truncated_but_keeps_the_reason(store, owner):
    record.enable(store, owner.id, "remem")
    three_events(store, owner)

    class ProseExtractor:
        def extract(self, events, project, known_titles=None):
            return parse_entries("z" * 5000)

    extraction.process(store, owner.id, ProseExtractor(),
                       idle_seconds=IDLE, limit=10)

    [job] = store.recent_failed_extract_jobs(owner.id)
    assert "no JSON array" in job.error
    assert job.error.count("z") == extraction.MAX_RAW_IN_ERROR


def test_a_write_that_explodes_is_recorded_not_raised(store, owner):
    """A database error mid-write must be recorded against the job, and the
    loop must keep going rather than stranding it in `running`."""
    record.enable(store, owner.id, "remem")
    three_events(store, owner)

    class ExplodingStore:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def put_entry(self, entry):
            raise RuntimeError("connection lost")

    report = extraction.process(
        ExplodingStore(store), owner.id, FakeExtractor([an_entry()]),
        idle_seconds=IDLE, limit=10,
    )

    assert (report.claimed, report.failed) == (1, 1)
    [job] = store.recent_failed_extract_jobs(owner.id)
    assert "connection lost" in job.error


def test_one_failing_session_does_not_stop_the_others(store, owner):
    record.enable(store, owner.id, "remem")
    three_events(store, owner, session_id="a")
    three_events(store, owner, session_id="b", base=NOW - timedelta(hours=3))

    class HalfBroken:
        def __init__(self):
            self.calls = 0

        def extract(self, events, project, known_titles=None):
            self.calls += 1
            if self.calls == 1:
                raise ExtractionFailed("claude exploded", "raw")
            return [an_entry()]

    report = extraction.process(store, owner.id, HalfBroken(),
                                idle_seconds=IDLE, limit=10)

    assert (report.claimed, report.succeeded, report.failed) == (2, 1, 1)
    assert report.entries_written == 1


def test_process_on_an_idle_store_reports_nothing_claimed(store, owner):
    report = extraction.process(store, owner.id, FakeExtractor([]),
                                idle_seconds=IDLE, limit=10)
    assert (report.claimed, report.succeeded, report.failed,
            report.entries_written) == (0, 0, 0, 0)


def test_the_limit_bounds_one_run(store, owner):
    record.enable(store, owner.id, "remem")
    for i, session in enumerate(("a", "b", "c")):
        three_events(store, owner, session_id=session,
                     base=NOW - timedelta(hours=2 + i))

    report = extraction.process(store, owner.id, FakeExtractor([]),
                                idle_seconds=IDLE, limit=2)

    assert report.claimed == 2


def test_repeated_success_never_exhausts_the_attempt_budget(conn, store, owner):
    """The counter is a *consecutive*-failure budget, and success clears it.

    Discovery-based claiming re-claims the SAME row every time a session
    produces new outstanding events, so `attempts` is incremented by runs
    that succeed. Left unreset it is a lifetime claim counter, and a session
    that is extracted cleanly more times than MAX_ATTEMPTS - a resumed one,
    or a long one worked over successive runs by MAX_EVENTS_PER_JOB - dies
    with "gave up after N attempts", a reason that never happened.
    """
    record.enable(store, owner.id, "remem")
    rounds = extraction.MAX_ATTEMPTS + 3

    for i in range(rounds):
        record.record(
            store,
            owner.id,
            a_harness_event(
                occurred_at=NOW - timedelta(hours=3) + timedelta(minutes=i),
                payload={"command": f"round {i}"},
            ),
            "claude-code",
        )
        report = extraction.process(
            store, owner.id, FakeExtractor([an_entry(title=f"round {i}")]),
            idle_seconds=IDLE, limit=10,
        )
        assert (report.claimed, report.succeeded, report.failed) == (1, 1, 0), (
            f"round {i}: {report}"
        )

    [job] = _all_jobs(conn, store, owner)
    assert job.status is JobStatus.DONE
    assert job.error is None
    assert job.attempts <= extraction.MAX_ATTEMPTS
