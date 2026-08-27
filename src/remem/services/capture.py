"""Automatic capture: opt-in state, the job spool, and the drain.

Every decision about what gets captured lives here. The hook only enqueues and
the CLI only formats.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from remem.distill.base import CapturedEntry, Distiller, DistillationFailed
from remem.domain import CaptureJob, CaptureStatus, Origin, Query, new_id
from remem.services.write import remember
from remem.store import Store

MAX_ATTEMPTS = 3
AGENT = "claude-code"


@dataclass(slots=True)
class DrainReport:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    entries_written: int = 0


def enable(store: Store, owner_id: UUID, project: str) -> None:
    store.set_capture_enabled(owner_id, project, True)


def disable(store: Store, owner_id: UUID, project: str) -> None:
    store.set_capture_enabled(owner_id, project, False)


def is_enabled(store: Store, owner_id: UUID, project: str) -> bool:
    return store.capture_enabled(owner_id, project)


def enqueue(
    store: Store,
    owner_id: UUID,
    *,
    project: str,
    transcript_path: str,
    session_id: str | None,
) -> CaptureJob | None:
    """Queue a session for distillation, or return None if capture is off.

    Opt-in is the safety gate: nothing accumulates from a project the user did
    not choose.
    """
    if not store.capture_enabled(owner_id, project):
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


def _already_captured(store: Store, owner_id: UUID, project: str, title: str) -> bool:
    """True when this project already has a live CAPTURED entry with this title.

    Only prior captures suppress a capture. A human-written entry with the same
    title is not a duplicate to swallow silently.
    """
    hits = store.search(
        Query(project=project, origins=[Origin.CAPTURE], limit=200), owner_id
    )
    return any(h.entry.title == title for h in hits)


def _write(
    store: Store, owner_id: UUID, job: CaptureJob, entries: list[CapturedEntry]
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
            origin=Origin.CAPTURE,
        )
        written += 1
    return written


def drain(
    store: Store, owner_id: UUID, distiller: Distiller, limit: int = 10
) -> DrainReport:
    """Distil claimed jobs. Never raises: a failing job records its reason."""
    report = DrainReport()
    for job in store.claim_capture_jobs(owner_id, limit=limit):
        report.claimed += 1
        if job.attempts > MAX_ATTEMPTS:
            store.finish_capture_job(
                job.id, owner_id, CaptureStatus.FAILED,
                f"gave up after {MAX_ATTEMPTS} attempts", 0,
            )
            report.failed += 1
            continue
        try:
            text = Path(job.transcript_path).read_text(errors="replace")
        except OSError as exc:
            store.finish_capture_job(
                job.id, owner_id, CaptureStatus.FAILED,
                f"transcript unreadable at {job.transcript_path}: {exc}", 0,
            )
            report.failed += 1
            continue
        try:
            entries = distiller.distill(text, job.project)
        except DistillationFailed as exc:
            store.finish_capture_job(
                job.id, owner_id, CaptureStatus.FAILED, str(exc)[:500], 0
            )
            report.failed += 1
            continue
        except Exception as exc:  # a distiller is third-party-ish code
            store.finish_capture_job(
                job.id, owner_id, CaptureStatus.FAILED,
                f"distiller raised {type(exc).__name__}: {exc}"[:500], 0,
            )
            report.failed += 1
            continue

        written = _write(store, owner_id, job, entries)
        store.finish_capture_job(
            job.id, owner_id, CaptureStatus.DONE, None, written
        )
        report.succeeded += 1
        report.entries_written += written
    return report
