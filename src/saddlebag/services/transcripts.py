"""Transcript capture: every policy decision for storing raw session traces.

Frontends parse and format; they never decide. The rules that live here -
what a claim means, when a file is re-read, what counts as an anomaly, how
much a spawned refresh may do - are ones every frontend gets for free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from saddlebag.domain import Transcript, TranscriptTrigger
from saddlebag.store import Store
from saddlebag.transcript_file import ReadPlan, classify, parse, sha256_hex

__all__ = [
    "Candidate",
    "PathRefused",
    "Report",
    "REFRESH_FILE_CAP",
    "discover",
    "designate",
    "undesignate",
    "run",
]

#: The harness whose transcripts this reads. A constant rather than a
#: parameter because there is exactly one today and inventing the
#: generalisation before a second harness exists would be guessing at its
#: shape - the `harness` COLUMN is the part that costs nothing to have early.
HARNESS = "claude-code"


@dataclass
class Candidate:
    """A directory discovery believes belongs to a project, with its evidence."""

    path: str
    #: Transcripts in this directory whose session id has recorded events for
    #: this project. This is the proof; the name of the directory is not.
    matched: int
    #: Every transcript in the directory. Deliberately reported beside
    #: `matched`, because claiming imports all of them - including sessions
    #: from before recording existed, which can outnumber the matched ones
    #: several times over.
    total: int
    claimed_by: str | None


def discover(store: Store, owner_id: UUID, project: str, root: Path) -> list[Candidate]:
    """Propose directories that hold this project's sessions. Writes nothing.

    Ownership is PROVEN, not guessed: a transcript's filename is a session
    id, and `events` already records which project each session belongs to.
    Matching on the directory slug instead would have missed 184MB of this
    project's own history, because the tool was renamed partway through.

    The floor, stated rather than papered over: a directory whose sessions
    were never recorded cannot be found here at all. That is why this
    proposes and `designate` decides.
    """
    recorded = set(store.event_session_ids(owner_id, project))
    if not recorded or not root.is_dir():
        return []

    claims = {p.path: p.project for p in store.transcript_paths(owner_id)}

    found: list[Candidate] = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        files = sorted(directory.glob("*.jsonl"))
        if not files:
            continue
        matched = sum(1 for f in files if f.stem in recorded)
        if matched == 0:
            continue
        found.append(
            Candidate(
                path=str(directory),
                matched=matched,
                total=len(files),
                claimed_by=claims.get(str(directory)),
            )
        )
    return found


class PathRefused(Exception):
    """A directory cannot be claimed, and the message says why."""


def designate(store: Store, owner_id: UUID, project: str, path: Path) -> str:
    """Claim a transcript directory for a project. Fail-loud by design.

    This is the second opt-in and it deserves saying plainly: the per-project
    record gate governs recording going FORWARD, while claiming a directory
    imports all of it - including sessions that predate the pipeline
    entirely. Nothing auto-claims, which is why this refuses loudly rather
    than skipping: it is the one moment there is a human to tell.

    The path is stored absolute and resolved, deliberately unlike
    `reingest designate`, which stores repo-relative paths against a git
    root. These directories are outside any repository.
    """
    if not path.exists():
        raise PathRefused(f"{path} does not exist")
    if not path.is_dir():
        raise PathRefused(f"{path} is not a directory")

    absolute = str(path.resolve())
    holder = store.add_transcript_path(owner_id, project, absolute)
    if holder is not None:
        raise PathRefused(
            f"{absolute} is already claimed by project '{holder}' - "
            f"a directory belongs to one project, or the same session would "
            f"be filed under two"
        )
    return absolute


def undesignate(store: Store, owner_id: UUID, project: str, path: Path) -> bool:
    """Drop a claim. Transcripts already imported are NOT deleted.

    Same reasoning as the import's refusal to follow a shrunk file: this
    command stops future reading, and destroying stored sessions is a
    separate, explicit act. `bag transcripts prune` is the thing that would
    delete, and it does not exist yet.
    """
    return store.remove_transcript_path(owner_id, project, str(path.resolve()))


#: How many files a spawned refresh may read in one run.
#:
#: The first import of a claimed directory is 179MB across 146 files, which
#: must never happen inside a session-start hook. The cap is applied BEFORE
#: reading rather than after, so a bounded run is bounded in I/O and not
#: merely in what it reports. The typed `bag transcripts import` passes None
#: and does everything, because a person asked for that.
REFRESH_FILE_CAP = 25


@dataclass
class Report:
    """What one import did. Mutated in place so a partial run is recorded."""

    files_seen: int = 0
    files_new: int = 0
    files_appended: int = 0
    files_rebuilt: int = 0
    lines_written: int = 0
    bytes_written: int = 0
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)


def run(
    store: Store,
    owner_id: UUID,
    project: str,
    *,
    trigger: TranscriptTrigger,
    cap: int | None = None,
) -> Report:
    """Import every claimed directory for a project, recording the run.

    Split into a recording wrapper and `_run_body` for the reason
    `memory.sync` is: the wrapper owns the row, and the body is handed the
    `Report` it mutates, so a run that raises partway is still recorded with
    what it had done. A Python exception is recorded as a failure with path
    `*` and re-raised.

    The caller MUST open its `store` with `autocommit=True`, the same rule
    `bag reingest run` and `bag memory sync` already follow. `start_transcript_run`
    has to be committed before any file is read - otherwise a raise from deep
    in `_run_body` (a psycopg error, most likely) leaves the connection in a
    failed transaction, and the `finish_transcript_run` call below raises
    `InFailedSqlTransaction` instead of running, silently replacing the real
    exception and recording nothing. Stated here so it is inherited rather
    than rediscovered by every future caller.
    """
    report = Report()
    started = store.start_transcript_run(owner_id, project, trigger)
    try:
        _run_body(store, owner_id, project, cap, report)
    except Exception as exc:
        report.failures.append({"path": "*", "reason": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        store.finish_transcript_run(
            started.id,
            owner_id,
            files_seen=report.files_seen,
            files_new=report.files_new,
            files_appended=report.files_appended,
            files_rebuilt=report.files_rebuilt,
            lines_written=report.lines_written,
            bytes_written=report.bytes_written,
            anomalies=report.anomalies,
            failures=report.failures,
        )
    return report


def _run_body(
    store: Store,
    owner_id: UUID,
    project: str,
    cap: int | None,
    report: Report,
) -> None:
    budget = cap
    for claim in store.transcript_paths(owner_id, project):
        directory = Path(claim.path)
        if not directory.is_dir():
            # A claimed directory that has gone is reported on every run,
            # which is how a moved or deleted directory stops being a silent
            # per-run no-op. Same treatment ingest gives a missing path.
            report.failures.append(
                {"path": claim.path, "reason": "claimed directory does not exist"}
            )
            continue
        for path in sorted(directory.glob("*.jsonl")):
            if budget is not None and budget <= 0:
                return
            report.files_seen += 1
            did_work = _import_one(store, owner_id, project, path, report)
            if did_work and budget is not None:
                budget -= 1


def _import_one(
    store: Store,
    owner_id: UUID,
    project: str,
    path: Path,
    report: Report,
) -> bool:
    """Import or update one transcript. True when it did any reading.

    The budget is spent only on files that were actually read, so a refresh
    over a directory of unchanged transcripts costs one stat each and skips
    nothing it could have done.
    """
    session_id = path.stem
    existing = store.get_transcript(owner_id, HARNESS, session_id)

    try:
        disk_size = path.stat().st_size
    except OSError as exc:
        report.failures.append({"path": str(path), "reason": str(exc)})
        return False

    if existing is None:
        return _store_whole(store, owner_id, project, path, report, new=True)

    plan = _plan_for(existing, path, disk_size, report)
    if plan is ReadPlan.SKIP or plan is ReadPlan.SHRUNK:
        return False
    if plan is ReadPlan.REBUILD:
        return _store_whole(store, owner_id, project, path, report, new=False)
    return _append(store, owner_id, existing, path, report)


def _plan_for(
    existing: Transcript, path: Path, disk_size: int, report: Report
) -> ReadPlan:
    """Classify, reading the prefix only when the size actually grew."""
    prefix_sha: str | None = None
    if disk_size > existing.bytes:
        try:
            with path.open("rb") as fh:
                prefix_sha = sha256_hex(fh.read(existing.bytes))
        except OSError as exc:
            report.failures.append({"path": str(path), "reason": str(exc)})
            return ReadPlan.SKIP

    plan = classify(existing.bytes, existing.sha256, disk_size, prefix_sha)
    if plan is ReadPlan.SHRUNK:
        # Recorded and NOT followed. The stored copy is more complete than
        # what is on disk, and the purpose of the source row is that a
        # rotating file does not destroy the session.
        report.anomalies.append(
            {"path": str(path), "stored": existing.bytes, "on_disk": disk_size}
        )
    return plan


def _store_whole(
    store: Store,
    owner_id: UUID,
    project: str,
    path: Path,
    report: Report,
    *,
    new: bool,
) -> bool:
    try:
        content = path.read_bytes()
    except OSError as exc:
        report.failures.append({"path": str(path), "reason": str(exc)})
        return False

    stored = store.put_transcript(
        owner_id,
        project,
        HARNESS,
        path.stem,
        str(path),
        content,
        sha256_hex(content),
    )
    lines, failures = parse(content)
    store.replace_transcript_lines(stored.id, lines)

    report.lines_written += len(lines)
    report.bytes_written += len(content)
    report.failures.extend({"path": str(path), "reason": f.reason} for f in failures)
    if new:
        report.files_new += 1
    else:
        report.files_rebuilt += 1
    return True


def _append(
    store: Store,
    owner_id: UUID,
    existing: Transcript,
    path: Path,
    report: Report,
) -> bool:
    try:
        with path.open("rb") as fh:
            fh.seek(existing.bytes)
            tail = fh.read()
    except OSError as exc:
        report.failures.append({"path": str(path), "reason": str(exc)})
        return False

    # Fetched once and used twice: for the whole-file hash that the NEXT
    # append check compares against, and for the tail's first line number.
    stored = store.transcript_content(existing.id, owner_id) or b""

    # seq is the line's number within the FILE, and `parse` numbers lines by
    # their index in `split(b"\n")` - which counts blank lines and lines that
    # failed to parse, because each still occupies a line in the file. The
    # stored ROW count counts neither, so using it here would drift seq by one
    # for every blank or unparseable line ever seen, silently - and
    # (transcript_id, seq) is the coordinate future labelling work keys on.
    # Counting newlines in the stored bytes is exact: the tail begins
    # immediately after the last stored byte, so its first line is line number
    # `stored.count(b"\n")`.
    start_seq = stored.count(b"\n")

    if not store.append_transcript(
        existing.id, owner_id, tail, sha256_hex(stored + tail)
    ):
        report.failures.append(
            {"path": str(path), "reason": "append refused - not this owner"}
        )
        return False

    lines, failures = parse(tail, start_seq=start_seq)
    store.add_transcript_lines(existing.id, lines)

    report.files_appended += 1
    report.lines_written += len(lines)
    report.bytes_written += len(tail)
    report.failures.extend({"path": str(path), "reason": f.reason} for f in failures)
    return True
