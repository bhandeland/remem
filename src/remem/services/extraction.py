"""Extraction: a quiet session's events in, entries and provenance out.

Every decision about what gets extracted lives here. The CLI only formats,
and the model only proposes - what is written, what is suppressed as a
duplicate, and what a failure records are all this module's calls.

There is no enqueue. Nothing queues a session for extraction: `process`
discovers sessions that have been quiet long enough through
`sessions_awaiting_extraction` and claims them. That idle trigger is why the
pipeline no longer needs a SessionEnd hook to work - two of the three
harnesses remem targets do not have one.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from remem.domain import Event, ExtractJob, JobStatus, Origin, Query
from remem.extract.base import ExtractedEntry, ExtractionFailed, Extractor
from remem.services.write import remember
from remem.store import Store

MAX_ATTEMPTS = 3

# The spec's diagnostic budget: the model's own output, as recorded against a
# failed job. The reason is kept whole in front of it - truncating the pair as
# one string would let a long raw response clip the reason away to nothing.
MAX_RAW_IN_ERROR = 500
MAX_REASON_IN_ERROR = 200

# The most events one run hands the model. The renderer bounds the prompt by
# bytes as well; this bounds the query, so a runaway session cannot make the
# read itself expensive.
MAX_EVENTS_PER_JOB = 500

# How many sessions discovery looks at per `limit` it will actually claim.
# See the comment in `process`: sessions that have given up are skipped, and
# without a window wider than the batch they would still crowd live sessions
# out of it.
DISCOVERY_OVERFETCH = 5


class ExtractJobNotFound(Exception):
    """No extract job with that id belongs to this owner."""

    def __init__(self, job_id: UUID):
        super().__init__(f"No extract job {job_id}")
        self.job_id = job_id


@dataclass(slots=True)
class ExtractReport:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    entries_written: int = 0


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
    extraction re-deriving a rule the user had written by hand, so those are
    exactly the titles the extractor most needs to know about.
    """
    hits = store.search(Query(project=project, limit=KNOWN_TITLE_LIMIT), owner_id)
    return [h.entry.title for h in hits]


def _extract(
    extractor: Extractor,
    events: list[Event],
    project: str,
    known_titles: list[str],
) -> list[ExtractedEntry]:
    """Call an extractor, tolerating one that predates `known_titles`.

    The protocol's third argument is optional; a two-argument implementation
    (including the fakes in the test suite) must keep working rather than
    failing with a TypeError that would be recorded as an extraction failure.
    """
    try:
        accepts = "known_titles" in inspect.signature(extractor.extract).parameters
    except (TypeError, ValueError):
        accepts = False
    if accepts:
        return extractor.extract(events, project, known_titles=known_titles)
    return extractor.extract(events, project)


def _already_extracted(
    store: Store, owner_id: UUID, project: str, title: str
) -> bool:
    """True when this project already has a live EXTRACTED entry with this title.

    Only prior extractions suppress an extraction. A human-written entry with
    the same title is not a duplicate to swallow silently.
    """
    hits = store.search(
        Query(project=project, origins=[Origin.EXTRACTED], limit=200), owner_id
    )
    return any(h.entry.title == title for h in hits)


def _write(
    store: Store,
    owner_id: UUID,
    job: ExtractJob,
    entries: list[ExtractedEntry],
    events: list[Event],
) -> int:
    """Write the entries and the provenance behind them.

    Provenance is recorded per entry against EVERY event in the batch, not
    against a guessed subset. The extractor does not say which event produced
    which entry, and inventing an attribution would put a false fact in an
    audit table. "This entry came out of these events" is what we know.
    """
    written = 0
    for entry in entries:
        if _already_extracted(store, owner_id, job.project, entry.title):
            continue
        stored = remember(
            store,
            owner_id,
            title=entry.title,
            body=entry.body,
            kind=entry.kind,
            project=job.project,
            tags=list(entry.tags),
            agent=job.harness,
            session_id=job.session_id,
            origin=Origin.EXTRACTED,
        )
        store.link_entry_events(stored.id, events, owner_id)
        written += 1
    return written


def _safe_finish(
    store: Store,
    job: ExtractJob,
    owner_id: UUID,
    status: JobStatus,
    error: str | None,
    written: int,
    covers_through: datetime | None,
) -> bool:
    """Record a job's outcome. Never raises.

    If the database is unreachable we cannot record anything - but the run
    must still finish its loop and return a report rather than raising into
    the CLI. The returned bool means "no exception", and nothing stronger:
    `finish_extract_job` silently updates no rows when the id and owner match
    nothing, so a finish that matched nothing looks exactly like one that
    worked. That is the store's contract and this is not the layer to change
    it - every job id here came out of a claim moments earlier.
    """
    try:
        store.finish_extract_job(
            job.id, owner_id, status, error, written, covers_through
        )
        return True
    except Exception:
        return False


def _run_job(
    store: Store,
    owner_id: UUID,
    extractor: Extractor,
    job: ExtractJob,
    *,
    enforce_cap: bool = True,
) -> tuple[bool, int]:
    """Extract from one already-claimed job and record its outcome. Never raises.

    Returns (succeeded, entries_written). `enforce_cap=False` is for a
    deliberate retry by id, where giving up is precisely what the user is
    overriding.

    The watermark is preserved on every failure path: `finish_extract_job`
    writes `covers_through` unconditionally, so passing None on a failure
    would erase an earlier successful run's mark and re-extract events that
    have already produced entries.
    """
    mark = job.covers_through
    try:
        if enforce_cap and job.attempts > MAX_ATTEMPTS:
            _safe_finish(
                store, job, owner_id, JobStatus.FAILED,
                f"gave up after {MAX_ATTEMPTS} attempts", 0, mark,
            )
            return False, 0
        try:
            # `mark` is the watermark of the last run that finished: only the
            # events after it are outstanding. A resumed session is therefore
            # extracted from where the last run stopped, not from its start.
            events = store.events_for_session(
                owner_id, job.project, job.harness, job.session_id,
                since=mark, limit=MAX_EVENTS_PER_JOB,
            )
        except Exception as exc:
            _safe_finish(
                store, job, owner_id, JobStatus.FAILED,
                f"could not read events: {type(exc).__name__}: {exc}"[:500],
                0, mark,
            )
            return False, 0

        if not events:
            # A session whose only events were filtered out, or one already
            # extracted to its end, is a quiet session - not a broken one.
            _safe_finish(store, job, owner_id, JobStatus.DONE, None, 0, mark)
            return True, 0

        try:
            known = _known_titles(store, owner_id, job.project)
            entries = _extract(extractor, events, job.project, known)
        except ExtractionFailed as exc:
            _safe_finish(
                store, job, owner_id, JobStatus.FAILED,
                _failure_reason(exc), 0, mark,
            )
            return False, 0
        except Exception as exc:  # an extractor is third-party-ish code
            _safe_finish(
                store, job, owner_id, JobStatus.FAILED,
                f"extractor raised {type(exc).__name__}: {exc}"[:500], 0, mark,
            )
            return False, 0

        written = _write(store, owner_id, job, entries, events)
        _safe_finish(
            store, job, owner_id, JobStatus.DONE, None, written,
            max(e.occurred_at for e in events),
        )
        return True, written
    except Exception as exc:
        # A backstop beneath the specific handlers above: any other
        # unexpected failure (e.g. the store itself raising mid-write)
        # must still be recorded against this job, and the caller must
        # move on rather than stranding it in `running`.
        _safe_finish(
            store, job, owner_id, JobStatus.FAILED,
            f"{type(exc).__name__}: {exc}"[:500], 0, mark,
        )
        return False, 0


def _gave_up(job: ExtractJob | None) -> bool:
    """True when this session's job has already given up and must be skipped.

    Discovery is what makes this necessary, and it is not how the capture
    spool behaved: `claim_capture_jobs` only ever took pending and stale
    rows, so a job that gave up simply dropped out of the backlog.
    `sessions_awaiting_extraction` computes its watermarks from DONE jobs
    only, so a session whose job failed still has outstanding events and is
    rediscovered by every later run - which would claim it again, record
    "gave up" again, and report `failed 1` for the life of the session.

    Worse than the noise: discovery is ordered oldest-event-first, so dead
    sessions sort AHEAD of live ones. Ten of them fill a default `--limit 10`
    batch and no new session is ever extracted again. Giving up has to
    remove the session from the backlog, not merely stop calling the model.

    Skipped, not tallied: there was no work, and a run with nothing to do
    must be able to say so. `process_job` is the deliberate way back.
    """
    return (
        job is not None
        and job.status is JobStatus.FAILED
        and job.attempts > MAX_ATTEMPTS
    )


def _tally(report: ExtractReport, succeeded: bool, written: int) -> None:
    report.claimed += 1
    if succeeded:
        report.succeeded += 1
        report.entries_written += written
    else:
        report.failed += 1


def process(
    store: Store,
    owner_id: UUID,
    extractor: Extractor,
    idle_seconds: int,
    limit: int = 10,
) -> ExtractReport:
    """Extract from every session quiet for `idle_seconds`, up to `limit`.

    A failing job records its reason and the loop continues to the next
    session - one bad session must not strand the rest of the backlog. A
    database failure while discovering or claiming sessions does propagate:
    at that point there is nothing left to record an outcome against, and a
    cron run that swallowed it would report a clean sweep of nothing.
    """
    report = ExtractReport()
    # Over-fetch, then stop at `limit` sessions actually claimed. Skipping a
    # dead session after discovery keeps it from being re-run, but on its own
    # it still lets one occupy a slot in the batch - and dead sessions sort
    # first (see _gave_up), so a small `--limit` would be filled by them
    # while live sessions waited behind. The window is bounded rather than
    # unbounded: a backlog deeper in dead sessions than this is a state worth
    # noticing in `status`, not one worth scanning the whole events table for.
    fetched = store.sessions_awaiting_extraction(
        owner_id, idle_seconds, limit * DISCOVERY_OVERFETCH
    )
    for session in fetched:
        if _gave_up(store.extract_job_for_session(owner_id, session)):
            continue
        job = store.claim_extract_job(owner_id, session)
        _tally(report, *_run_job(store, owner_id, extractor, job))
        if report.claimed >= limit:
            break
    return report


def process_job(
    store: Store, owner_id: UUID, job_id: UUID, extractor: Extractor
) -> ExtractReport:
    """Extract one named job, cap or no cap.

    `process` only ever claims sessions with outstanding events, so a job
    that has given up is otherwise unreachable - visible in the status
    command and unactionable. This is the retry.

    Raises ExtractJobNotFound when no such job belongs to this owner; that is
    a caller error worth reporting, not a job outcome to record.
    """
    if store.get_extract_job(job_id, owner_id) is None:
        raise ExtractJobNotFound(job_id)
    job = store.claim_extract_job_by_id(job_id, owner_id)
    if job is None:
        raise ExtractJobNotFound(job_id)
    report = ExtractReport()
    _tally(report, *_run_job(store, owner_id, extractor, job, enforce_cap=False))
    return report
