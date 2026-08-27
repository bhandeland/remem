import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.distill.base import CapturedEntry, DistillationFailed
from remem.domain import CaptureStatus, Kind, Origin, Query
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
