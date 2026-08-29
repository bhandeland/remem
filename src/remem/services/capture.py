"""Automatic capture: opt-in state, the job spool, and the drain.

Every decision about what gets captured lives here. The hook only enqueues and
the CLI only formats.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from remem.extract.base import ExtractedEntry, Extractor, ExtractionFailed
from remem.domain import CaptureJob, CaptureStatus, Origin, Query, new_id
from remem.services.write import remember
from remem.store import Store

MAX_ATTEMPTS = 3
AGENT = "claude-code"

# The spec's diagnostic budget: the model's own output, as recorded against a
# failed job. The reason is kept whole in front of it - truncating the pair as
# one string would let a long raw response clip the reason away to nothing.
MAX_RAW_IN_ERROR = 500
MAX_REASON_IN_ERROR = 200


class CaptureJobNotFound(Exception):
    """No capture job with that id belongs to this owner."""

    def __init__(self, job_id: UUID):
        super().__init__(f"No capture job {job_id}")
        self.job_id = job_id


@dataclass(slots=True)
class DrainReport:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    entries_written: int = 0


def enable(store: Store, owner_id: UUID, project: str) -> None:
    store.set_record_enabled(owner_id, project, True)


def disable(store: Store, owner_id: UUID, project: str) -> None:
    store.set_record_enabled(owner_id, project, False)


def is_enabled(store: Store, owner_id: UUID, project: str) -> bool:
    return store.record_enabled(owner_id, project)


def enqueue(
    store: Store,
    owner_id: UUID,
    *,
    project: str,
    transcript_path: str,
    session_id: str | None,
) -> CaptureJob | None:
    """Queue a session for extraction, or return None if capture is off.

    Opt-in is the safety gate: nothing accumulates from a project the user did
    not choose.
    """
    if not store.record_enabled(owner_id, project):
        return None
    return store.enqueue_capture(
        CaptureJob(
            id=new_id(),
            owner_id=owner_id,
            project=project,
            transcript_path=transcript_path,
            session_id=session_id,
        )
    )


def _failure_reason(exc: ExtractionFailed) -> str:
    """Compose a job error from the reason plus the model's raw output.

    An LLM returning prose instead of JSON is this feature's commonest real
    failure, and the reason alone ("no JSON array in extractor output") cannot
    tell a refusal from a truncation from a wrong shape.
    """
    reason = str(exc)[:MAX_REASON_IN_ERROR]
    raw = (getattr(exc, "raw", None) or "").strip()
    if not raw:
        return reason
    return f"{reason}; raw output: {raw[:MAX_RAW_IN_ERROR]}"


KNOWN_TITLE_LIMIT = 200


def _known_titles(store: Store, owner_id: UUID, project: str) -> list[str]:
    """Titles already recorded for this project, whoever wrote them.

    Human-written entries are included deliberately: the observed failure was
    capture re-deriving a rule the user had written by hand, so those are
    exactly the titles the extractor most needs to know about.
    """
    hits = store.search(Query(project=project, limit=KNOWN_TITLE_LIMIT), owner_id)
    return [h.entry.title for h in hits]


def _extract(
    extractor: Extractor, transcript: str, project: str, known_titles: list[str]
) -> list[ExtractedEntry]:
    """Call a extractor, tolerating one that predates `known_titles`.

    The protocol gained an optional argument; a two-argument implementation
    (including the fakes in the test suite) must keep working rather than
    failing with a TypeError that would be recorded as a extraction failure.
    """
    try:
        accepts = "known_titles" in inspect.signature(extractor.extract).parameters
    except (TypeError, ValueError):
        accepts = False
    if accepts:
        return extractor.extract(transcript, project, known_titles=known_titles)
    return extractor.extract(transcript, project)


def _already_captured(store: Store, owner_id: UUID, project: str, title: str) -> bool:
    """True when this project already has a live CAPTURED entry with this title.

    Only prior captures suppress a capture. A human-written entry with the same
    title is not a duplicate to swallow silently.
    """
    hits = store.search(
        Query(project=project, origins=[Origin.EXTRACTED], limit=200), owner_id
    )
    return any(h.entry.title == title for h in hits)


def _write(
    store: Store, owner_id: UUID, job: CaptureJob, entries: list[ExtractedEntry]
) -> int:
    written = 0
    for entry in entries:
        if _already_captured(store, owner_id, job.project, entry.title):
            continue
        remember(
            store,
            owner_id,
            title=entry.title,
            body=entry.body,
            kind=entry.kind,
            project=job.project,
            tags=list(entry.tags),
            agent=AGENT,
            session_id=job.session_id,
            origin=Origin.EXTRACTED,
        )
        written += 1
    return written


def _safe_finish(
    store: Store,
    job: CaptureJob,
    owner_id: UUID,
    status: CaptureStatus,
    error: str | None,
    written: int,
) -> bool:
    """Record a job's outcome. Never raises.

    If the database is unreachable we cannot record anything - but the drain
    must still finish its loop and return a report rather than raising into
    the CLI or the hook.
    """
    try:
        store.finish_capture_job(job.id, owner_id, status, error, written)
        return True
    except Exception:
        return False


def _run_job(
    store: Store,
    owner_id: UUID,
    extractor: Extractor,
    job: CaptureJob,
    *,
    enforce_cap: bool = True,
) -> tuple[bool, int]:
    """Distil one already-claimed job and record its outcome. Never raises.

    Returns (succeeded, entries_written). `enforce_cap=False` is for a
    deliberate retry by id, where giving up is precisely what the user is
    overriding.
    """
    try:
        if enforce_cap and job.attempts > MAX_ATTEMPTS:
            _safe_finish(
                store, job, owner_id, CaptureStatus.FAILED,
                f"gave up after {MAX_ATTEMPTS} attempts", 0,
            )
            return False, 0
        try:
            text = Path(job.transcript_path).read_text(errors="replace")
        except OSError as exc:
            _safe_finish(
                store, job, owner_id, CaptureStatus.FAILED,
                f"transcript unreadable at {job.transcript_path}: {exc}", 0,
            )
            return False, 0
        try:
            known = _known_titles(store, owner_id, job.project)
            entries = _extract(extractor, text, job.project, known)
        except ExtractionFailed as exc:
            _safe_finish(
                store, job, owner_id, CaptureStatus.FAILED,
                _failure_reason(exc), 0,
            )
            return False, 0
        except Exception as exc:  # a extractor is third-party-ish code
            _safe_finish(
                store, job, owner_id, CaptureStatus.FAILED,
                f"extractor raised {type(exc).__name__}: {exc}"[:500], 0,
            )
            return False, 0

        written = _write(store, owner_id, job, entries)
        _safe_finish(store, job, owner_id, CaptureStatus.DONE, None, written)
        return True, written
    except Exception as exc:
        # A backstop beneath the specific handlers above: any other
        # unexpected failure (e.g. the store itself raising mid-write)
        # must still be recorded against this job, and the caller must
        # move on rather than stranding it in `running`.
        _safe_finish(
            store, job, owner_id, CaptureStatus.FAILED,
            f"{type(exc).__name__}: {exc}"[:500], 0,
        )
        return False, 0


def _tally(report: DrainReport, succeeded: bool, written: int) -> None:
    report.claimed += 1
    if succeeded:
        report.succeeded += 1
        report.entries_written += written
    else:
        report.failed += 1


def drain(
    store: Store, owner_id: UUID, extractor: Extractor, limit: int = 10
) -> DrainReport:
    """Distil claimed jobs. Never raises: a failing job records its reason."""
    report = DrainReport()
    for job in store.claim_capture_jobs(owner_id, limit=limit):
        _tally(report, *_run_job(store, owner_id, extractor, job))
    return report


def drain_job(
    store: Store, owner_id: UUID, job_id: UUID, extractor: Extractor
) -> DrainReport:
    """Distil one named job, cap or no cap.

    `drain` only ever claims pending and stale-running jobs, so a job that
    has given up is otherwise unreachable - visible in `capture status` and
    unactionable. This is the retry.

    Raises CaptureJobNotFound when no such job belongs to this owner; that is
    a caller error worth reporting, not a job outcome to record.
    """
    if store.get_capture_job(job_id, owner_id) is None:
        raise CaptureJobNotFound(job_id)
    job = store.claim_capture_job(job_id, owner_id)
    if job is None:
        raise CaptureJobNotFound(job_id)
    report = DrainReport()
    _tally(report, *_run_job(store, owner_id, extractor, job, enforce_cap=False))
    return report
