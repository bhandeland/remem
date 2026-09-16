"""Transcript capture: every policy decision for storing raw session traces.

Frontends parse and format; they never decide. The rules that live here -
what a claim means, when a file is re-read, what counts as an anomaly, how
much a spawned refresh may do - are ones every frontend gets for free.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from saddlebag.agents.claude_code.memory import projects_dir
from saddlebag.domain import (
    Transcript,
    TranscriptPath,
    TranscriptRun,
    TranscriptTrigger,
)
from saddlebag.store import Store
from saddlebag.transcript_file import ReadPlan, classify, parse, sha256_hex

__all__ = [
    "Candidate",
    "PathRefused",
    "PathStatus",
    "Report",
    "REFRESH_FILE_CAP",
    "TranscriptFile",
    "TranscriptStatus",
    "advisories",
    "designate",
    "discover",
    "run",
    "status",
    "status_to_dict",
    "transcript_files",
    "transcript_root",
    "undesignate",
]

#: The harness whose transcripts this reads. A constant rather than a
#: parameter because there is exactly one today and inventing the
#: generalisation before a second harness exists would be guessing at its
#: shape - the `harness` COLUMN is the part that costs nothing to have early.
HARNESS = "claude-code"


def transcript_root() -> Path:
    """Where this harness keeps its transcripts, honouring CLAUDE_CONFIG_DIR.

    Here rather than in `cli.py` for the reason `HARNESS` is: which
    directory holds Claude Code's transcripts is a fact about the harness,
    not about a command, and a frontend that builds it by hand is a
    frontend deciding. Two copies of `~/.claude/projects` had already grown
    in `cli.py`, and both were silently wrong for anyone who sets
    `CLAUDE_CONFIG_DIR` - `discover` proposed nothing with no evidence and
    no error, and `status` called every recorded session irrecoverable,
    which is precisely the number that exists to argue for importing
    sooner.

    The resolution itself is delegated to the adapter module that already
    owns it (`agents/claude_code/memory.projects_dir`, which `memory_dir`
    also resolves through) rather than re-spelled here: a third copy of the
    variable name is how the next override lands in one answer and not the
    other.

    `discover` and `status` still TAKE `root` as a parameter - what moved
    is the default, not the seam, so a test can still point them at a
    `tmp_path`.
    """
    return projects_dir()


#: Claude Code's layout below a session directory. Module constants so the
#: one function that reads the layout names it once - tests deliberately do
#: NOT import these, and spell the layout literally instead.
SUBAGENT_DIR = "subagents"
SUBAGENT_PREFIX = "agent-"
META_SUFFIX = ".meta.json"


@dataclass(frozen=True)
class TranscriptFile:
    """One transcript on disk, and the identity its PATH gives it.

    Identity is read from the path and never from the contents: a refresh
    has to decide what a file is from a stat, before it reads a byte.
    Measured 2026-09-16, every line's `sessionId` and `agentId` agreed with
    the path in all 392 subagent files on this machine.
    """

    path: Path
    #: For a subagent file this is the PARENT's session id - which is what
    #: the file's own lines say, and what `events` records its tool calls
    #: under.
    session_id: str
    #: None for a session's own transcript; the `agentId` for a subagent's.
    agent_id: str | None
    #: The subagent's `agent-<id>.meta.json`, when one sits beside it. Always
    #: None for a session's own transcript.
    meta: Path | None = None


def transcript_files(directory: Path) -> list[TranscriptFile]:
    """Every transcript in a claimed directory, sessions and subagents both.

    The ONLY place that knows the layout. The first version of this feature
    globbed `*.jsonl` in four places, none of them recursive, and all
    four were blind to `<session>/subagents/agent-<id>.jsonl` - more bytes
    than the sessions themselves, and the only copy of those conversations
    (the parent transcript holds none of their lines). One owner is what
    stops a fourth caller reintroducing the blind spot.

    `tool-results/` sits beside `subagents/` and is hook stdout, not a
    transcript, so the subagent pattern is anchored on its directory name
    rather than recursing. `agent-<id>.meta.json` sits beside each subagent
    transcript and rides on that transcript's entry as `meta`, paired by
    name from one glob per directory, so this still stats nothing. A
    sidecar with no transcript is not returned: none existed when this was
    written (2026-09-16, 432 sidecars), and the row it would attach to is
    the transcript's.

    Sorted on identity, not on the Path: `Path` ordering compares parts, so
    `s1` sorts before `s1.jsonl` and every subagent would come ahead of its
    own parent. Nothing here stats a file - a bounded refresh counts those.
    """
    found = [TranscriptFile(p, p.stem, None) for p in directory.glob("*.jsonl")]
    sidecars = set(directory.glob(f"*/{SUBAGENT_DIR}/{SUBAGENT_PREFIX}*{META_SUFFIX}"))
    for p in directory.glob(f"*/{SUBAGENT_DIR}/{SUBAGENT_PREFIX}*.jsonl"):
        meta = p.with_name(p.stem + META_SUFFIX)
        found.append(
            TranscriptFile(
                p,
                p.parent.parent.name,
                p.stem.removeprefix(SUBAGENT_PREFIX),
                meta if meta in sidecars else None,
            )
        )
    found.sort(key=lambda f: (f.session_id, f.agent_id is not None, f.agent_id or ""))  # type: ignore[implicit-any-lambda]
    return found


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
    #: Subagent files under this directory's sessions. Not evidence - they
    #: carry their parent's session id, so they prove nothing `matched` has
    #: not - but a claim imports them, and `total` exists to say what a
    #: claim commits someone to reading.
    subagents: int
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
        files = transcript_files(directory)
        sessions = [f for f in files if f.agent_id is None]
        if not sessions:
            continue
        matched = sum(1 for f in sessions if f.session_id in recorded)
        if matched == 0:
            continue
        found.append(
            Candidate(
                path=str(directory),
                matched=matched,
                total=len(sessions),
                subagents=len(files) - len(sessions),
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
    metas_written: int = 0
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

    A project with no claimed directory writes nothing at all - not even a
    run row. `bag transcripts refresh` is spawned at every session start,
    for whatever project the session is in, and most projects will never
    claim a transcript directory: without this, every one of those sessions
    would leave a `transcript_runs` row recording that nothing happened,
    forever. That is the same contract `bag memory refresh` already holds
    for an undesignated project - the common case must cost nothing and
    record nothing. A project that HAS claimed a directory still gets a row
    on every run, even one where the directory has since been deleted: that
    project opted in, and a reader needs to see its state, including that
    the claim has gone missing.
    """
    claims = store.transcript_paths(owner_id, project)
    if not claims:
        return Report()

    report = Report()
    started = store.start_transcript_run(owner_id, project, trigger)
    try:
        _run_body(store, owner_id, project, cap, claims, report)
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
            metas_written=report.metas_written,
            anomalies=report.anomalies,
            failures=report.failures,
        )
    return report


def _run_body(
    store: Store,
    owner_id: UUID,
    project: str,
    cap: int | None,
    claims: list[TranscriptPath],
    report: Report,
) -> None:
    # Built ONCE per run, not once per file: a directory can hold hundreds
    # of transcripts and the recorded projects are one query for all of
    # them. This is the same (session id -> project) fact `discover` proves
    # ownership with, asked without pinning the project - because the whole
    # question below is whether the recorded project and the claiming one
    # disagree.
    recorded: dict[str, set[str]] = {}
    for session_id, recorded_project in store.event_session_projects(owner_id):
        recorded.setdefault(session_id, set()).add(recorded_project)

    budget = cap
    for claim in claims:
        directory = Path(claim.path)
        if not directory.is_dir():
            # A claimed directory that has gone is reported on every run,
            # which is how a moved or deleted directory stops being a silent
            # per-run no-op. Same treatment ingest gives a missing path.
            report.failures.append(
                {"path": claim.path, "reason": "claimed directory does not exist"}
            )
            continue
        for file in transcript_files(directory):
            if budget is not None and budget <= 0:
                return
            report.files_seen += 1
            existing = store.get_transcript(
                owner_id, HARNESS, file.session_id, file.agent_id
            )
            did_work = _import_one(
                store, owner_id, project, file, existing, report, recorded
            )
            if did_work and budget is not None:
                budget -= 1
            # After the transcript, whatever its plan - and outside the
            # budget. See `_import_meta`.
            _import_meta(store, owner_id, file, existing, report)


def _import_one(
    store: Store,
    owner_id: UUID,
    project: str,
    file: TranscriptFile,
    existing: Transcript | None,
    report: Report,
    recorded: dict[str, set[str]],
) -> bool:
    """Import or update one transcript. True when it did any reading.

    The budget is spent only on files that were actually read, so a refresh
    over a directory of unchanged transcripts costs one stat each and skips
    nothing it could have done.

    Identity arrives on `file` and is never re-derived from `path.stem`:
    for a subagent the stem is `agent-<id>`, which is neither the session
    nor, alone, the agent - and both places that once read the stem would
    have gone wrong without raising.
    """
    path = file.path

    try:
        disk_size = path.stat().st_size
    except OSError as exc:
        report.failures.append({"path": str(path), "reason": str(exc)})
        return False

    _check_project_agreement(project, file, report, recorded)

    if existing is None:
        return _store_whole(store, owner_id, project, file, report, new=True)

    plan = _plan_for(existing, path, disk_size, report)
    if plan is ReadPlan.SHRUNK:
        return False
    if plan is ReadPlan.SKIP:
        # The derived-lines repair. `transcript_lines` is written in a
        # separate transaction from the content (both CLI paths open with
        # autocommit=True, which the run row genuinely needs), so a Ctrl-C
        # during a long typed backfill - or a line Postgres refuses as
        # jsonb, a NUL byte inside a string being the realistic one - can
        # leave the bytes stored and the lines empty. Nothing else would
        # ever notice: the file has not changed, so every later run stats
        # it, classifies SKIP, and it stays empty forever. The source bytes
        # are never at risk, but "derived and rebuildable" is only true if
        # something actually rebuilds.
        #
        # The guard is `> 0` and deliberately NOT a comparison against an
        # expected count. A torn final line and a `do nothing` seq conflict
        # are both known, accepted fidelity warts that leave fewer rows
        # than the file has lines, so an exact comparison would re-parse
        # those files on EVERY run, forever. Zero is the only value that
        # unambiguously means "the derived half never landed". Do not tidy
        # this into the stricter check.
        #
        # It costs one indexed count per unchanged file, which is cheap
        # beside the stat it sits next to, and it self-heals at the next
        # session start with no human having to notice a silent condition.
        if store.transcript_line_count(existing.id) > 0:
            return False
        return _store_whole(store, owner_id, project, file, report, new=False)
    if plan is ReadPlan.REBUILD:
        return _store_whole(store, owner_id, project, file, report, new=False)
    return _append(store, owner_id, existing, path, report)


def _import_meta(
    store: Store,
    owner_id: UUID,
    file: TranscriptFile,
    existing: Transcript | None,
    report: Report,
) -> None:
    """Store a subagent's sidecar when the file has one and the row has none.

    That one condition is both the backfill and the steady state. It runs
    after the transcript whatever its plan - SKIP and SHRUNK included -
    because every row imported before migration 025 classifies SKIP, and a
    check living on the new/append paths would never reach one of them.

    Read once, never again. Measured 2026-09-16, no sidecar on the machine
    was modified more than a second after it was created, so there is
    nothing to detect and no change detection. If Claude Code starts
    rewriting them, this is the rule to revisit. A sidecar that vanishes
    leaves the stored copy alone, the same as a shrunk transcript.

    Validated as a JSON object and then stored RAW. Because nothing
    overwrites, a sidecar caught mid-write would otherwise be kept torn
    forever; refused, it is a failure this run and is tried again next run.

    It spends none of the refresh cap. The cap bounds transcript I/O and a
    sidecar is a few hundred bytes - spending it would take ten session
    starts to backfill 70KB.
    """
    if file.meta is None or (existing is not None and existing.has_meta):
        return
    # A row stored this run is fetched again for its id. A new subagent is
    # the only case that reaches here, which is rare after the first import.
    row = existing or store.get_transcript(
        owner_id, HARNESS, file.session_id, file.agent_id
    )
    if row is None:
        # The transcript's own read failed and is already reported. Its
        # sidecar waits for the run that stores it.
        return
    try:
        meta = file.meta.read_bytes()
    except OSError as exc:
        report.failures.append({"path": str(file.meta), "reason": str(exc)})
        return
    try:
        parsed = json.loads(meta)
    except ValueError as exc:
        report.failures.append(
            {"path": str(file.meta), "reason": f"sidecar is not JSON: {exc}"}
        )
        return
    if not isinstance(parsed, dict):
        report.failures.append(
            {"path": str(file.meta), "reason": "sidecar is not a JSON object"}
        )
        return
    if not store.set_transcript_meta(row.id, owner_id, meta):
        report.failures.append(
            {"path": str(file.meta), "reason": "sidecar refused - not this owner"}
        )
        return
    report.metas_written += 1


#: Why an entry is in `Report.anomalies`. The list carries more than one
#: shape now, so every entry says which it is rather than leaving a reader
#: to infer it from which keys arrived.
SHRANK = "shrank"
PROJECT_CONFLICT = "project-conflict"


def _check_project_agreement(
    project: str,
    file: TranscriptFile,
    report: Report,
    recorded: dict[str, set[str]],
) -> None:
    """Report, but do not act on, a session recorded under another project.

    The spec requires this: a directory can hold sessions from more than one
    project if the working directory moved, and filing a trace under the
    wrong project silently is exactly the guess it forbids. Without it the
    move was invisible from both sides - `stored_transcripts`, `backlog` and
    `status` are all project-scoped, so the losing project's counts simply
    dropped.

    The file is stored ANYWAY and the anomaly stands. The bytes are the
    scarce thing here - a session Claude Code has since deleted cannot be
    fetched again - and refusing to store them to protect a label would
    trade the irreplaceable half for the repairable one. The label is made
    stable instead: `put_transcript` no longer overwrites `project` on
    conflict, so a transcript keeps the project it was first filed under and
    a human decides.

    A session with no recorded events says nothing at all - most claimed
    directories hold sessions from before recording existed - so only a
    recorded project that DISAGREES is an anomaly. Multiple recorded
    projects for one session are reported as they are found rather than
    resolved: picking one would be the guess.

    Subagent files are checked against their PARENT's session id, which is
    what `events` records their tool calls under, and each gets its own
    entry - each really is a file stored under a disputed label. The entry
    names the session and agent so `advisories` can count sessions rather
    than files.
    """
    known = recorded.get(file.session_id)
    if not known or project in known:
        return
    report.anomalies.append(
        {
            "reason": PROJECT_CONFLICT,
            "path": str(file.path),
            "session_id": file.session_id,
            "agent_id": file.agent_id,
            "claiming": project,
            "recorded": sorted(known),
        }
    )


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
            {
                "reason": SHRANK,
                "path": str(path),
                "stored": existing.bytes,
                "on_disk": disk_size,
            }
        )
    return plan


def _store_whole(
    store: Store,
    owner_id: UUID,
    project: str,
    file: TranscriptFile,
    report: Report,
    *,
    new: bool,
) -> bool:
    path = file.path
    try:
        content = path.read_bytes()
    except OSError as exc:
        report.failures.append({"path": str(path), "reason": str(exc)})
        return False

    stored = store.put_transcript(
        owner_id,
        project,
        HARNESS,
        file.session_id,
        str(path),
        content,
        sha256_hex(content),
        agent_id=file.agent_id,
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


@dataclass
class PathStatus:
    """A claimed directory, and whether it is still there."""

    path: str
    present: bool
    on_disk: int
    #: Subagent files, counted apart from `on_disk` (sessions) - folding
    #: them together is how a status that read "clean" once hid them.
    subagents: int


@dataclass
class TranscriptStatus:
    """What `bag transcripts status` answers for one project.

    `run` describes what last HAPPENED; `paths`, `backlog` and
    `irrecoverable` describe the state NOW. A reader must not have to infer
    one from the other, which is why both are here rather than only the run.
    """

    project: str
    paths: list[PathStatus]
    run: TranscriptRun | None
    backlog: int
    subagent_backlog: int
    #: Stored subagent rows with a sidecar on disk and none stored. An
    #: unstored subagent is already in `subagent_backlog` and is not
    #: counted twice.
    meta_backlog: int
    irrecoverable: int


def status(store: Store, owner_id: UUID, project: str, root: Path) -> TranscriptStatus:
    """The current state of transcript capture for one project."""
    claims = store.transcript_paths(owner_id, project)
    paths: list[PathStatus] = []
    on_disk: set[tuple[str, str | None]] = set()
    with_meta: set[tuple[str, str | None]] = set()
    for claim in claims:
        directory = Path(claim.path)
        present = directory.is_dir()
        files: list[TranscriptFile] = transcript_files(directory) if present else []
        on_disk.update((f.session_id, f.agent_id) for f in files)
        with_meta.update((f.session_id, f.agent_id) for f in files if f.meta)
        subagents = sum(1 for f in files if f.agent_id is not None)
        paths.append(
            PathStatus(
                path=claim.path,
                present=present,
                on_disk=len(files) - subagents,
                subagents=subagents,
            )
        )

    rows = store.stored_transcripts(owner_id, project)
    stored = {(t.session_id, t.agent_id) for t in rows}
    meta_stored = {(t.session_id, t.agent_id) for t in rows if t.has_meta}
    # Sessions whose OWN transcript is stored. A stored subagent does not
    # make its lost parent recoverable, and `recorded` is a set of sessions.
    stored_sessions = {session for session, agent in stored if agent is None}
    recorded = set(store.event_session_ids(owner_id, project))

    # "Anywhere" means anywhere under the transcript root, not only under a
    # claimed directory: a session whose file sits in an unclaimed directory
    # is recoverable by claiming it, and calling that irrecoverable would
    # overstate the loss. Session files only, for the same reason as above.
    everywhere = {f.stem for f in root.glob("*/*.jsonl")} if root.is_dir() else set()

    missing = on_disk - stored
    return TranscriptStatus(
        project=project,
        paths=paths,
        run=store.latest_transcript_run(owner_id, project),
        backlog=sum(1 for _, agent in missing if agent is None),
        subagent_backlog=sum(1 for _, agent in missing if agent is not None),
        meta_backlog=len((with_meta & stored) - meta_stored),
        irrecoverable=len(recorded - stored_sessions - everywhere),
    )


def status_to_dict(got: TranscriptStatus) -> dict[str, Any]:
    """One object, not a list, and never a shorter document.

    The keys are the same in every state - an unclaimed project is a null
    `run` and an empty `paths`, not fewer keys - so a consumer checks a key
    for null rather than branching on which keys arrived. Timestamps are a
    raw `isoformat()`: the offset travels in the string.
    """
    return {
        "project": got.project,
        "paths": [
            {
                "path": p.path,
                "present": p.present,
                "on_disk": p.on_disk,
                "subagents": p.subagents,
            }
            for p in got.paths
        ],
        "run": None if got.run is None else _run_to_dict(got.run),
        "backlog": got.backlog,
        "subagent_backlog": got.subagent_backlog,
        "meta_backlog": got.meta_backlog,
        "irrecoverable": got.irrecoverable,
    }


def _run_to_dict(run: TranscriptRun) -> dict[str, Any]:
    """`TranscriptRun` as plain JSON, mirroring `ingest._run_to_dict` field
    for field. A raw `.isoformat()` rather than a local conversion: the
    offset travels in the string, and nothing here reads it beside a
    human-formatted line the way `render_run` does."""
    return {
        "id": str(run.id),
        "trigger": str(run.trigger),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "files_seen": run.files_seen,
        "files_new": run.files_new,
        "files_appended": run.files_appended,
        "files_rebuilt": run.files_rebuilt,
        "lines_written": run.lines_written,
        "bytes_written": run.bytes_written,
        "metas_written": run.metas_written,
        "anomalies": list(run.anomalies),
        "failures": list(run.failures),
    }


def advisories(store: Store, owner_id: UUID) -> list[str]:
    """One line per unhealthy claimed project, for `bag record status`.

    Sweeps EVERY claimed project, which it can because a claim stores an
    absolute path and needs no recorded working directory to resolve - unlike
    `ingest.status`, which can only check the project the current directory
    resolves to.

    Backlog is deliberately not here. A refresh is bounded before it reads,
    so a nonzero backlog is the normal state between runs, and an advisory
    that fires on every run is one people learn to ignore.

    No leading "!" here, unlike the brief's prose sketch of these lines:
    `bag record status` adds that marker itself (`events.render`), the same
    way it does for the doctor, ingest, memory and kb advisory lines - a
    caller that prefixed its own would double it up there.
    """
    lines: list[str] = []
    by_project: dict[str, list[str]] = {}
    for claim in store.transcript_paths(owner_id):
        by_project.setdefault(claim.project, []).append(claim.path)

    for project, paths in sorted(by_project.items()):
        missing = [p for p in paths if not Path(p).is_dir()]
        if missing:
            lines.append(
                f"transcripts '{project}': claimed directory missing "
                f"({', '.join(missing)}) - run `bag transcripts status`"
            )
            continue

        run = store.latest_transcript_run(owner_id, project)
        if run is None:
            lines.append(
                f"transcripts '{project}': claimed but never imported - "
                f"run `bag transcripts import`"
            )
        elif run.finished_at is None:
            lines.append(
                f"transcripts '{project}': the last import ({run.trigger}) "
                f"did not finish - run `bag transcripts status`"
            )
        elif run.failures:
            lines.append(
                f"transcripts '{project}': {len(run.failures)} failure(s) in "
                f"the last import ({run.trigger}) - run `bag transcripts status`"
            )
        elif run.anomalies:
            # Counted by reason rather than lumped together: "shrank on
            # disk" and "recorded under another project" are different
            # things to go and look at, and one advisory naming only the
            # first would send a reader to the wrong screen. A row written
            # before anomalies carried a `reason` falls into `other` and is
            # counted rather than dropped.
            shrank = sum(1 for a in run.anomalies if a.get("reason") == SHRANK)
            conflict_entries = [
                a for a in run.anomalies if a.get("reason") == PROJECT_CONFLICT
            ]
            # Sessions, not files: one session with 57 subagent files is one
            # thing to go and look at. An entry written before anomalies
            # named their session falls back to its path, so it is still
            # counted rather than collapsed into a phantom None session.
            conflicts = len(
                {a.get("session_id") or a.get("path") for a in conflict_entries}
            )
            parts = []
            if shrank:
                parts.append(f"{shrank} transcript(s) shrank on disk")
            if conflicts:
                parts.append(f"{conflicts} session(s) recorded under another project")
            other = len(run.anomalies) - shrank - len(conflict_entries)
            if other:
                parts.append(f"{other} anomaly(ies)")
            lines.append(
                f"transcripts '{project}': {', '.join(parts)} - "
                f"run `bag transcripts status`"
            )
    return lines
