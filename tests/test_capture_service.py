import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.distill.base import CapturedEntry, DistillationFailed, parse_entries
from remem.domain import CaptureStatus, Kind, Origin, Query, new_id
from remem.services import capture

pytestmark = pytest.mark.db


class FakeDistiller:
    """Stands in for claude -p. No test spawns a real LLM."""

    def __init__(self, entries=None, error=None):
        self._entries = entries or []
        self._error = error
        self.calls = []

    def distill(self, transcript, project):
        self.calls.append((transcript, project))
        if self._error:
            raise self._error
        return list(self._entries)


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


@pytest.fixture
def transcript(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"role":"user","content":"hello"}\n')
    return str(p)


def test_enqueue_returns_none_when_capture_is_disabled(store, owner, transcript):
    assert capture.enqueue(store, owner.id, project="remem",
                           transcript_path=transcript, session_id="s") is None


def test_enqueue_creates_a_job_when_enabled(store, owner, transcript):
    capture.enable(store, owner.id, "remem")
    job = capture.enqueue(store, owner.id, project="remem",
                          transcript_path=transcript, session_id="s")
    assert job is not None
    assert job.status is CaptureStatus.PENDING


def test_disable_stops_enqueueing(store, owner, transcript):
    capture.enable(store, owner.id, "remem")
    capture.disable(store, owner.id, "remem")
    assert capture.enqueue(store, owner.id, project="remem",
                           transcript_path=transcript, session_id="s") is None


def test_drain_writes_entries_with_capture_origin(store, owner, transcript):
    capture.enable(store, owner.id, "remem")
    capture.enqueue(store, owner.id, project="remem",
                    transcript_path=transcript, session_id="sess-9")
    distiller = FakeDistiller([
        CapturedEntry(title="Pool sizing", body="pgbouncer saturates",
                      kind=Kind.MEMORY, tags=["ops"])
    ])

    report = capture.drain(store, owner.id, distiller)
    assert report.succeeded == 1
    assert report.entries_written == 1

    hits = store.search(Query(text="pgbouncer"), owner.id)
    assert [h.entry.title for h in hits] == ["Pool sizing"]
    entry = hits[0].entry
    assert entry.origin is Origin.CAPTURE
    assert entry.project == "remem"
    assert entry.session_id == "sess-9"
    assert entry.agent == "claude-code"


def test_drain_passes_the_transcript_contents_to_the_distiller(
    store, owner, transcript
):
    capture.enable(store, owner.id, "remem")
    capture.enqueue(store, owner.id, project="remem",
                    transcript_path=transcript, session_id="s")
    distiller = FakeDistiller([])
    capture.drain(store, owner.id, distiller)
    assert "hello" in distiller.calls[0][0]


def test_an_empty_distillation_is_a_success(store, owner, transcript):
    """Most sessions contain nothing durable; that must not read as failure."""
    capture.enable(store, owner.id, "remem")
    job = capture.enqueue(store, owner.id, project="remem",
                          transcript_path=transcript, session_id="s")
    report = capture.drain(store, owner.id, FakeDistiller([]))
    assert report.succeeded == 1
    assert report.failed == 0
    assert store.get_capture_job(job.id, owner.id).status is CaptureStatus.DONE


def test_a_distillation_failure_is_recorded_not_raised(store, owner, transcript):
    capture.enable(store, owner.id, "remem")
    job = capture.enqueue(store, owner.id, project="remem",
                          transcript_path=transcript, session_id="s")
    report = capture.drain(
        store, owner.id, FakeDistiller(error=DistillationFailed("claude exploded"))
    )
    assert report.failed == 1
    stored = store.get_capture_job(job.id, owner.id)
    assert stored.status is CaptureStatus.FAILED
    assert "claude exploded" in stored.error


def test_a_missing_transcript_fails_with_a_readable_reason(store, owner):
    capture.enable(store, owner.id, "remem")
    job = capture.enqueue(store, owner.id, project="remem",
                          transcript_path="/nonexistent/t.jsonl", session_id="s")
    capture.drain(store, owner.id, FakeDistiller([]))
    stored = store.get_capture_job(job.id, owner.id)
    assert stored.status is CaptureStatus.FAILED
    assert "transcript" in stored.error.lower()


def test_one_failing_job_does_not_stop_the_others(store, owner, transcript):
    capture.enable(store, owner.id, "remem")
    capture.enqueue(store, owner.id, project="remem",
                    transcript_path="/nonexistent.jsonl", session_id="a")
    capture.enqueue(store, owner.id, project="remem",
                    transcript_path=transcript, session_id="b")
    report = capture.drain(store, owner.id, FakeDistiller([]))
    assert report.claimed == 2
    assert report.failed == 1
    assert report.succeeded == 1


def test_dedup_skips_a_title_already_captured_for_that_project(
    store, owner, transcript
):
    """Capture re-derives the same facts every session; without this the store
    fills with near-identical entries."""
    capture.enable(store, owner.id, "remem")
    entry = CapturedEntry(title="Pool sizing", body="first", kind=Kind.MEMORY)

    for session in ("a", "b"):
        capture.enqueue(store, owner.id, project="remem",
                        transcript_path=transcript, session_id=session)
        capture.drain(store, owner.id, FakeDistiller([entry]))

    hits = store.search(Query(text="Pool sizing"), owner.id)
    assert len(hits) == 1


def test_dedup_does_not_block_the_same_title_in_another_project(
    store, owner, transcript
):
    capture.enable(store, owner.id, "alpha")
    capture.enable(store, owner.id, "beta")
    entry = CapturedEntry(title="Pool sizing", body="b", kind=Kind.MEMORY)

    for project in ("alpha", "beta"):
        capture.enqueue(store, owner.id, project=project,
                        transcript_path=transcript, session_id="s")
        capture.drain(store, owner.id, FakeDistiller([entry]))

    assert len(store.search(Query(text="Pool sizing"), owner.id)) == 2


def test_dedup_does_not_block_a_title_a_human_wrote(store, owner, transcript):
    """Only prior CAPTURES suppress a capture; a human entry is not a duplicate
    to be silently swallowed."""
    from remem.services.write import remember

    remember(store, owner.id, title="Pool sizing", body="human wrote this",
             project="remem", origin=Origin.HUMAN)
    capture.enable(store, owner.id, "remem")
    capture.enqueue(store, owner.id, project="remem",
                    transcript_path=transcript, session_id="s")
    capture.drain(
        store, owner.id,
        FakeDistiller([CapturedEntry(title="Pool sizing", body="captured",
                                     kind=Kind.MEMORY)]),
    )
    assert len(store.search(Query(text="Pool sizing"), owner.id)) == 2


def test_drain_gives_up_after_the_attempt_cap(store, owner):
    """A job that can never succeed must stop being retried, and say so.

    `claim` increments attempts before the cap is checked, so attempts itself
    rises past MAX_ATTEMPTS; what matters is that the drain stops calling the
    distiller and records that it gave up.
    """
    capture.enable(store, owner.id, "remem")
    job = capture.enqueue(store, owner.id, project="remem",
                          transcript_path="/nonexistent.jsonl", session_id="s")
    distiller = FakeDistiller([])

    for _ in range(capture.MAX_ATTEMPTS + 2):
        store.finish_capture_job(job.id, owner.id, CaptureStatus.PENDING, None, 0)
        capture.drain(store, owner.id, distiller)

    stored = store.get_capture_job(job.id, owner.id)
    assert stored.status is CaptureStatus.FAILED
    assert "gave up" in stored.error
    # The distiller is never reached for a job whose transcript is missing.
    assert distiller.calls == []


def test_drain_on_an_empty_queue_reports_nothing_claimed(store, owner):
    report = capture.drain(store, owner.id, FakeDistiller([]))
    assert report.claimed == 0


def test_drain_does_not_raise_when_writing_an_entry_fails(store, owner, transcript):
    """A database error mid-write must be recorded, not propagated."""
    capture.enable(store, owner.id, "remem")
    job = capture.enqueue(store, owner.id, project="remem",
                          transcript_path=transcript, session_id="s")

    class ExplodingStore:
        def __init__(self, inner): self._inner = inner
        def __getattr__(self, name): return getattr(self._inner, name)
        def put_entry(self, entry): raise RuntimeError("connection lost")

    report = capture.drain(
        ExplodingStore(store), owner.id,
        FakeDistiller([CapturedEntry(title="T", body="B", kind=Kind.MEMORY)]),
    )
    assert report.failed == 1
    stored = store.get_capture_job(job.id, owner.id)
    assert stored.status is CaptureStatus.FAILED
    assert "connection lost" in stored.error


def test_one_job_raising_does_not_abandon_the_rest(store, owner, transcript):
    """The loop must continue: jobs already claimed must not be stranded."""
    capture.enable(store, owner.id, "remem")
    capture.enqueue(store, owner.id, project="remem",
                    transcript_path=transcript, session_id="a")
    capture.enqueue(store, owner.id, project="remem",
                    transcript_path=transcript, session_id="b")

    calls = []

    class SometimesExploding:
        def __init__(self, inner): self._inner = inner
        def __getattr__(self, name): return getattr(self._inner, name)
        def put_entry(self, entry):
            calls.append(entry.title)
            if len(calls) == 1:
                raise RuntimeError("first one fails")
            return self._inner.put_entry(entry)

    report = capture.drain(
        SometimesExploding(store), owner.id,
        FakeDistiller([CapturedEntry(title="T", body="B", kind=Kind.MEMORY)]),
    )
    assert report.claimed == 2
    assert report.failed == 1
    assert report.succeeded == 1


def test_unparseable_output_records_the_raw_text_in_the_error(
    store, owner, transcript
):
    """Prose instead of JSON is the model's commonest failure. Show it."""
    prose = "I reviewed the session and found nothing worth remembering."
    capture.enable(store, owner.id, "remem")
    job = capture.enqueue(store, owner.id, project="remem",
                          transcript_path=transcript, session_id="s")

    class ProseDistiller:
        def distill(self, transcript, project):
            return parse_entries(prose)

    report = capture.drain(store, owner.id, ProseDistiller())

    assert report.failed == 1
    stored = store.get_capture_job(job.id, owner.id)
    assert stored.status is CaptureStatus.FAILED
    assert "no JSON array" in stored.error
    assert prose in stored.error


def test_a_long_raw_output_is_truncated_but_keeps_the_reason(
    store, owner, transcript
):
    capture.enable(store, owner.id, "remem")
    job = capture.enqueue(store, owner.id, project="remem",
                          transcript_path=transcript, session_id="s")

    class ProseDistiller:
        def distill(self, transcript, project):
            return parse_entries("z" * 5000)

    capture.drain(store, owner.id, ProseDistiller())

    stored = store.get_capture_job(job.id, owner.id)
    assert "no JSON array" in stored.error
    assert stored.error.count("z") == 500


def test_drain_job_retries_a_job_that_gave_up(store, owner, transcript):
    """The attempt cap is exactly what --job ID exists to override."""
    capture.enable(store, owner.id, "remem")
    job = capture.enqueue(store, owner.id, project="remem",
                          transcript_path=transcript, session_id="s")
    store.finish_capture_job(
        job.id, owner.id, CaptureStatus.FAILED, "gave up after 3 attempts", 0
    )

    report = capture.drain_job(
        store, owner.id, job.id,
        FakeDistiller([CapturedEntry(title="T", body="B", kind=Kind.MEMORY)]),
    )

    assert report.claimed == 1
    assert report.succeeded == 1
    assert report.entries_written == 1
    stored = store.get_capture_job(job.id, owner.id)
    assert stored.status is CaptureStatus.DONE
    assert stored.error is None


def test_drain_job_leaves_the_other_jobs_alone(store, owner, transcript):
    capture.enable(store, owner.id, "remem")
    mine = capture.enqueue(store, owner.id, project="remem",
                           transcript_path=transcript, session_id="a")
    other = capture.enqueue(store, owner.id, project="remem",
                            transcript_path=transcript, session_id="b")

    capture.drain_job(store, owner.id, mine.id, FakeDistiller([]))

    assert store.get_capture_job(other.id, owner.id).status is (
        CaptureStatus.PENDING
    )


def test_drain_job_records_a_failure_rather_than_raising(store, owner):
    capture.enable(store, owner.id, "remem")
    job = capture.enqueue(store, owner.id, project="remem",
                          transcript_path="/nonexistent.jsonl", session_id="s")

    report = capture.drain_job(store, owner.id, job.id, FakeDistiller([]))

    assert report.failed == 1
    assert store.get_capture_job(job.id, owner.id).status is CaptureStatus.FAILED


def test_drain_job_rejects_an_unknown_id(store, owner):
    with pytest.raises(capture.CaptureJobNotFound):
        capture.drain_job(store, owner.id, new_id(), FakeDistiller([]))


def test_drain_job_rejects_another_owners_job(store, owner, transcript):
    """Owner scoping is not optional just because an id was supplied."""
    other = store.ensure_principal("someone-else")
    capture.enable(store, other.id, "remem")
    job = capture.enqueue(store, other.id, project="remem",
                          transcript_path=transcript, session_id="s")

    with pytest.raises(capture.CaptureJobNotFound):
        capture.drain_job(store, owner.id, job.id, FakeDistiller([]))
