"""Loading markdown documents into remem as chunk-sized entries.

Every policy decision about ingest lives here: what a chunk's identity is,
when a chunk is rewritten rather than left alone, and which origin it gets.
The CLI passes paths and prints counts.

Identity is a pair of tags, `src:<path>` and `sec:<slug>`, following the
handoff precedent - a tag convention plus supersede, no new table. There is
no content hash: to know whether a chunk changed, compare its body to the
stored one. A hash would need somewhere to live and would answer the same
question, less directly.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from uuid import UUID

from remem.domain import (
    Entry,
    IngestDesignation,
    IngestRun,
    IngestTrigger,
    Kind,
    Origin,
    Query,
)
from remem.embed import Embedder
from remem.markdown import Chunk, split
from remem.services.embed import backfill_if_pending
from remem.services.write import remember, supersede
from remem.store import Store

#: The sweep lists a file's live chunks through `Query.limit`. Sweeping a
#: silently truncated list would supersede live chunks at random, which is
#: the worst failure available here - so exceeding this raises instead.
MAX_CHUNKS_PER_FILE = 200


class TooManyChunks(Exception):
    """A document produced more chunks than one sweep can safely list."""


class BadDesignation(Exception):
    """A designation names paths that cannot be stored as given."""


def designate(
    store: Store,
    owner_id: UUID,
    project: str,
    paths: list[str] | None,
    *,
    archive: bool = False,
) -> None:
    """Record which paths this project re-ingests, or clear them.

    Paths are refused unless they are repo-relative and stay inside the
    repository. The spawned refresh resolves them against the git root with
    nobody watching, so an absolute path would point at whatever the machine
    that stored it happened to have, and a `..` escape would silently ingest
    from outside the repository. Both are caught here, at the one moment
    there is a human to tell.

    `None` clears; an empty list does not. Clearing is a deliberate act and
    deserves its own spelling - an empty list reaching this far is a caller
    that built its argument wrong, and storing it would designate a project
    to ingest nothing, which reads identically to not being designated at all.
    """
    if paths is not None:
        if not paths:
            raise BadDesignation("No paths given. To clear a designation, pass None.")
        for raw in paths:
            path = PurePosixPath(Path(raw).as_posix())
            if path.is_absolute():
                raise BadDesignation(
                    f"{raw!r} is absolute. Designated paths are stored "
                    f"repo-relative and resolved against the git root."
                )
            if ".." in path.parts:
                raise BadDesignation(
                    f"{raw!r} escapes the repository. Designated paths must "
                    f"stay inside it."
                )
        paths = [PurePosixPath(Path(p).as_posix()).as_posix() for p in paths]
    store.set_ingest_paths(owner_id, project, paths, archive=archive)


def designations(
    store: Store, owner_id: UUID, project: str | None = None
) -> list[IngestDesignation]:
    """Every re-ingest designation, for one project or for all of them."""
    return store.ingest_designations(owner_id, project)


def relative_to_root(paths: list[Path], top: Path, cwd: Path) -> list[Path]:
    """Each path as the repository-relative identity `ingest_file` stores.

    Inside a repository, a chunk's `src:` tag is its path from the working
    tree's top level - the same identity the automatic refresh computes
    against the git root - regardless of where the command was typed or
    whether the argument was absolute. Before this, the tag was the path
    as typed, and `cd docs && remem ingest a.md` duplicated every chunk a
    root run had stored under `docs/a.md`.

    A path outside the repository is refused, loudly, because it has no
    root-relative identity and inventing one (the absolute path, say) is
    the duplication this exists to end. Same class as `designate`'s
    refusals, for the same reason: this is the moment there is a human to
    tell.

    Pure - takes `cwd` rather than reading it - so it is testable without
    changing directory.
    """
    top = top.resolve()
    out: list[Path] = []
    for raw in paths:
        located = (cwd / raw).resolve()
        try:
            out.append(located.relative_to(top))
        except ValueError:
            # Deliberately unconditional on project scope: --project and
            # --global do not bypass this. The refusal is about where the
            # file LIVES, not which project it would file under, so neither
            # flag changes the answer - and the message must not claim
            # otherwise, which it did until a review caught a user being
            # told to pass a flag that changes nothing.
            raise BadDesignation(
                f"{raw!s} is outside the repository at {top}. Inside a "
                f"repository, ingest identifies documents by their "
                f"repository-relative path; to ingest a file elsewhere, run "
                f"it from outside any repository."
            ) from None
    return out


@dataclass(slots=True)
class Report:
    created: int = 0
    changed: int = 0
    unchanged: int = 0
    swept: int = 0
    failures: list[tuple[Path, str]] = field(default_factory=list)
    #: (new path, existing src path, live chunks under it) - files that came
    #: in entirely new while an anchor with the same filename was already
    #: live under another src: tag. See `find_twin`. Advisory: the CLI
    #: prints them and exits 0, the refresh records them in its run row.
    twins: list[tuple[str, str, int]] = field(default_factory=list)

    def merge(self, other: Report) -> None:
        self.created += other.created
        self.changed += other.changed
        self.unchanged += other.unchanged
        self.swept += other.swept
        self.failures.extend(other.failures)
        self.twins.extend(other.twins)


def src_tag(path: Path) -> str:
    """The tag naming a chunk's source file.

    Posix-normalised so a Windows ingest and a macOS one agree about
    identity, which is what makes re-ingest work across machines.
    """
    return f"src:{Path(path).as_posix()}"


def sec_tag(slug: str) -> str:
    return f"sec:{slug}"


def _live_chunks(store: Store, owner_id: UUID, path: Path) -> list[Entry]:
    hits = store.search(
        Query(
            tags=[src_tag(path)],
            origins=[Origin.INGESTED, Origin.ARCHIVED],
            limit=MAX_CHUNKS_PER_FILE,
        ),
        owner_id,
    )
    if len(hits) >= MAX_CHUNKS_PER_FILE:
        raise TooManyChunks(
            f"{path} has {len(hits)} or more live chunks, at or above the "
            f"limit of {MAX_CHUNKS_PER_FILE}. Sweeping a truncated list would "
            f"supersede live entries at random."
        )
    return [h.entry for h in hits]


def _tags_for(path: Path, chunk: Chunk) -> list[str]:
    """The anchor carries no `sec:` tag - that absence is what identifies it."""
    tags = [src_tag(path)]
    if not chunk.anchor:
        tags.append(sec_tag(chunk.slug))
    return tags


def _sec_of(entry: Entry) -> str:
    """An entry's slug, or "" for the anchor."""
    for tag in entry.tags:
        if tag.startswith("sec:"):
            return tag[len("sec:") :]
    return ""


def _src_of(entry: Entry) -> str:
    """An entry's `src:` path, or "" when it has none."""
    for tag in entry.tags:
        if tag.startswith("src:"):
            return tag[len("src:") :]
    return ""


def find_twin(path: Path, anchors: Iterable[Entry]) -> tuple[str, Entry] | None:
    """An anchor whose src: path has the same filename as `path`, under a
    different path - or None.

    Same final component, not same stem: `a.md` and `a.txt` are two files.
    The file's own src: path is excluded because the caller only asks on
    the entirely-new branch, where it cannot be live - and if it somehow
    were, "you are your own twin" is not a useful warning.

    A moved file and a document ingested twice under two identities look
    identical from here, and the user knows which. So this returns a
    question, and nothing acts on the answer automatically.
    """
    own = Path(path).as_posix()
    name = PurePosixPath(own).name
    for anchor in anchors:
        src = _src_of(anchor)
        if src and src != own and PurePosixPath(src).name == name:
            return src, anchor
    return None


def ingest_file(
    store: Store,
    owner_id: UUID,
    path: Path,
    *,
    project: str | None,
    root: Path | None = None,
    archive: bool = False,
    dry_run: bool = False,
) -> Report:
    """Ingest one file.

    `root` separates the two jobs `path` was doing at once: where the file
    is READ and what IDENTIFIES it. With a root, the file is read at
    `root / path` while `src:` still records `path` - so an automatic run
    resolving against the git root produces the same identity a person got
    typing a repo-relative path, and sees their corpus as unchanged rather
    than duplicating it. Without one, both are the path as given, which is
    what the manual command has always done.
    """
    path = Path(path)
    source = root / path if root is not None else path
    chunks = split(source.read_text(), doc_name=path.stem)
    if len(chunks) > MAX_CHUNKS_PER_FILE:
        raise TooManyChunks(
            f"{path} produced {len(chunks)} chunks, over the limit of "
            f"{MAX_CHUNKS_PER_FILE}."
        )

    origin = Origin.ARCHIVED if archive else Origin.INGESTED
    existing = {_sec_of(e): e for e in _live_chunks(store, owner_id, path)}
    report = Report()
    seen: set[str] = set()
    anchor_id = None

    if not existing and project is not None:
        # Entirely new to remem under this identity. That is what a first
        # ingest looks like, and also what a second identity for a document
        # already here looks like - a moved file, or an absolute path once
        # and a relative one now. The two print identical counts, so this is
        # the one moment the difference can be pointed at. Read-only, so it
        # runs on a dry run too. Skipped without a project: anchors are
        # keyed on one, and a --global ingest has none.
        twin = find_twin(path, store.anchors(owner_id, project))
        if twin is not None:
            src, _ = twin
            try:
                live = len(_live_chunks(store, owner_id, Path(src)))
            except TooManyChunks:
                # An advisory check must not be able to fail the operation
                # it decorates - this count is for the twin's file, not the
                # new one being ingested, and letting it propagate would
                # attribute the failure to the wrong file and skip ingesting
                # it entirely. The twin's file is at or over the limit by
                # definition of the exception it just raised, so that count
                # is still an honest (if imprecise) thing to report.
                live = MAX_CHUNKS_PER_FILE
            report.twins.append((path.as_posix(), src, live))

    for chunk in chunks:
        seen.add(chunk.slug)
        current = existing.get(chunk.slug)
        if current is None:
            report.created += 1
            if not dry_run:
                written = remember(
                    store,
                    owner_id,
                    title=chunk.title,
                    body=chunk.body,
                    kind=Kind.DOC,
                    project=project,
                    origin=origin,
                    tags=_tags_for(path, chunk),
                )
                if chunk.anchor:
                    anchor_id = written.id
        elif current.body == chunk.body and current.title == chunk.title:
            # Skip entirely rather than rewrite an identical row: an update
            # would churn updated_at and make `remem embed` look like it has
            # work to redo when the stored vector is still correct.
            report.unchanged += 1
            if chunk.anchor:
                anchor_id = current.id
        else:
            # The title counts as much as the body here. It is half the
            # embedding text (services/embed.embed_text) and the highest
            # weighted field in the tsvector, so a chunk whose title moved
            # really is found differently and the stored vector really is
            # stale. It also has to: identity is (src, sec) and comparing
            # bodies alone would leave a renamed document's old titles
            # standing until each section's prose happened to change.
            report.changed += 1
            if not dry_run:
                replacement = supersede(
                    store,
                    owner_id,
                    current.id,
                    title=chunk.title,
                    body=chunk.body,
                )
                if chunk.anchor:
                    # The CURRENT anchor, not the retired row: if the anchor
                    # was itself superseded this run, orphans must point at
                    # its replacement or they would point at a dead entry.
                    anchor_id = replacement.id

    # The sweep. A heading that was renamed or deleted leaves a live chunk
    # with no counterpart in this reading of the file; left alone it stays
    # live and silently stale, returning alongside its own replacement with
    # nothing to say which is current.
    #
    # Orphans are superseded BY THE ANCHOR because set_superseded requires a
    # replacement id and a deleted heading has none. "This section is gone,
    # the document is here" is the honest reading, and it costs no schema
    # change - the alternative was a retired_at column plus a new predicate
    # in every query the store runs, for one caller.
    for slug, entry in existing.items():
        if slug in seen:
            continue
        report.swept += 1
        if dry_run or anchor_id is None:
            continue
        # store.set_superseded directly, NOT write.supersede: supersede
        # creates a replacement entry, and a swept chunk has no replacement -
        # that absence is the whole reason the anchor exists. Calling it here
        # would duplicate the orphan instead of retiring it.
        store.set_superseded(entry.id, anchor_id, owner_id)

    return report


def ingest_paths(
    store: Store,
    owner_id: UUID,
    paths: list[Path],
    *,
    project: str | None,
    root: Path | None = None,
    archive: bool = False,
    dry_run: bool = False,
) -> Report:
    """Ingest files and directories, collecting failures rather than aborting.

    One bad encoding in a directory must not cost every other file in it -
    this is a bulk command, and a caller who gets nothing back for one
    unreadable file learns less than one who gets the rest plus a named
    failure. The CLI exits non-zero when `failures` is non-empty.
    """
    report = Report()
    for path in _discover(paths, root):
        try:
            # ingest_file's changed branch is two statements - remember()
            # inserts the replacement, set_superseded() retires the old row
            # - and `refresh` runs its caller under autocommit so the run
            # row it started survives a crash. Without this wrap, a process
            # killed between those two statements leaves both rows live
            # under the same (src:, sec:) pair, which no later run can sweep
            # (see Store.transaction's docstring). Inside the existing try
            # so a failure mid-file still lands in report.failures rather
            # than aborting the whole path list.
            with store.transaction():
                report.merge(
                    ingest_file(
                        store,
                        owner_id,
                        path,
                        project=project,
                        root=root,
                        archive=archive,
                        dry_run=dry_run,
                    )
                )
        except (OSError, UnicodeDecodeError, TooManyChunks) as exc:
            report.failures.append((path, str(exc)))
    return report


def _discover(paths: list[Path], root: Path | None = None) -> list[Path]:
    """Files as given, directories globbed for **/*.md, sorted for a stable
    report. Sorted matters: a dry run the user reads and then re-runs for
    real must list its files in the same order both times.

    Every path returned is in the same frame as the paths passed in: with a
    root, globbing happens at `root / path` and the results are made
    relative to it again, so what comes back is still repo-relative and can
    be used as identity directly."""
    found: list[Path] = []
    for path in paths:
        path = Path(path)
        located = root / path if root is not None else path
        if not located.is_dir():
            found.append(path)
            continue
        hits = sorted(located.rglob("*.md"))
        found.extend([h.relative_to(root) for h in hits] if root is not None else hits)
    return found


@dataclass(slots=True)
class RefreshResult:
    """What one automatic re-ingest did, and what it could not do.

    `embed_error` is a string rather than an exception because the only
    caller that reads it is a fail-soft hook writing to stderr behind
    REMEM_HOOK_DEBUG. Keeping the count and the error separate is what lets
    "embedded nothing because there was nothing to embed" be told apart
    from "embedded nothing because there is no embedder".
    """

    report: Report = field(default_factory=Report)
    embedded: int = 0
    embed_error: str | None = None


def _outcome(report: Report, *, embedded: int, embed_error: str | None) -> dict:
    """A report as `Store.finish_ingest_run` keyword arguments.

    Paths become strings here, once, so the two callers that write a row
    cannot disagree about the stored shape.
    """
    return dict(
        created=report.created,
        changed=report.changed,
        unchanged=report.unchanged,
        swept=report.swept,
        embedded=embedded,
        failures=[
            {"path": Path(p).as_posix(), "reason": r} for p, r in report.failures
        ],
        twins=[{"path": p, "existing": e, "live": n} for p, e, n in report.twins],
        embed_error=embed_error,
    )


def ingest_manual(
    store: Store,
    owner_id: UUID,
    paths: list[Path],
    *,
    project: str | None,
    root: Path | None = None,
    archive: bool = False,
    dry_run: bool = False,
) -> Report:
    """`ingest_paths`, bracketed by a run row - what `remem ingest` calls.

    The row is what lets "when was this project last ingested at all" have
    one answer whether a person or the refresh did it. No row for a dry run
    (nothing happened) or without a project (the row is keyed on one, and
    a --global ingest has none). `ingest_paths` itself stays row-free
    because `refresh` calls it once per designation half and wraps the
    whole loop in a single row.

    Fail-loud like its caller: a failure to write the row is an error like
    any other, and the single transaction rolls the entries back with it.
    """
    if dry_run or project is None:
        return ingest_paths(
            store,
            owner_id,
            paths,
            project=project,
            root=root,
            archive=archive,
            dry_run=dry_run,
        )
    run = store.start_ingest_run(
        owner_id, project, IngestTrigger.MANUAL, archive=archive
    )
    report = ingest_paths(
        store, owner_id, paths, project=project, root=root, archive=archive
    )
    store.finish_ingest_run(
        run.id, owner_id, **_outcome(report, embedded=0, embed_error=None)
    )
    return report


def refresh(
    store: Store,
    owner_id: UUID,
    project: str,
    root: Path,
    *,
    embed_model: str,
    load_embedder: Callable[[], Embedder],
) -> RefreshResult:
    """Re-ingest this project's designated paths, then embed what is missing.

    The policy behind the detached job a session start spawns. An
    undesignated project does nothing at all - that silence is the opt-in,
    and it is also the common case, so it must cost nothing: no file is
    read, no embedder is built, and no run row is written.

    Paths are resolved against `root` (the git root) rather than the process
    working directory. The spawned job inherits whatever directory the
    harness happened to be in, which is not something a designation made
    weeks earlier can know.

    Everything this learns goes into a run row (017_ingest_runs.sql),
    because the caller is a detached process whose stderr is /dev/null: the
    row is the only record on the machine that this ran. It is started
    BEFORE any file is read so that a process which dies mid-run leaves a
    started, unfinished row - "crashed" rather than "never ran". That only
    holds if the started row is committed first, which is why `reingest
    run` opens its session with autocommit.

    A Python exception is a third case: recorded as a failure with path
    `*`, the row finished, and the exception re-raised for the caller's
    guard. The reason is kept in the row rather than lost to a debug
    channel nobody reads. A path that vanished lands in `report.failures`
    and an absent embedder in `embed_error`; neither raises.

    The "the reason is kept" promise above holds only when the caller runs
    autocommit, as `_reingest_once` does: the `except BaseException` below
    writes `finish_ingest_run` and re-raises, and a caller inside an open
    transaction would have that write rolled back along with everything
    else when the exception propagates.
    """
    result = RefreshResult()
    designated = store.ingest_designations(owner_id, project)
    if not designated:
        return result

    run = store.start_ingest_run(owner_id, project, IngestTrigger.AUTO)
    try:
        for designation in designated:
            result.report.merge(
                ingest_paths(
                    store,
                    owner_id,
                    [Path(p) for p in designation.paths],
                    project=project,
                    root=root,
                    archive=designation.archive,
                )
            )

        try:
            embedded = backfill_if_pending(store, owner_id, embed_model, load_embedder)
        except Exception as exc:
            # Losing the semantic tier is worth strictly less than the
            # entries just written, and `remem embed` remains the loud way
            # to find out that the embedder is broken.
            result.embed_error = str(exc)
        else:
            result.embedded = embedded.embedded if embedded else 0
    except BaseException as exc:
        # BaseException, matching the guard in `reingest run`: a
        # `typer.Exit` from a nested helper is a SystemExit, and a row that
        # says nothing about why it stopped is the gap this table closes.
        result.report.failures.append((Path("*"), f"{type(exc).__name__}: {exc}"))
        store.finish_ingest_run(
            run.id,
            owner_id,
            **_outcome(
                result.report, embedded=result.embedded, embed_error=result.embed_error
            ),
        )
        raise

    store.finish_ingest_run(
        run.id,
        owner_id,
        **_outcome(
            result.report, embedded=result.embedded, embed_error=result.embed_error
        ),
    )
    return result


# ---------------- status ----------------

#: Every advisory ends with this, scope and all. A pointer that leads to a
#: screen that contradicts the line teaches the user the line lies.
STATUS_POINTER = "see: remem reingest status --project {project}"


@dataclass(slots=True)
class ProjectIngestStatus:
    """What `remem reingest status` knows about one project.

    `checked_against` is None when the designated paths were NOT checked
    on disk - the project shown is not the one the current directory
    resolves to, so there is no root to check against. `missing` is then
    empty by construction, and the renderer says "not checked" rather
    than letting an empty list read as "all present".
    """

    project: str
    designations: list[IngestDesignation]
    last_run: IngestRun | None
    checked_against: Path | None
    missing: list[str]


def status(
    store: Store,
    owner_id: UUID,
    project: str | None,
    *,
    current_project: str | None,
    root: Path | None,
) -> list[ProjectIngestStatus]:
    """The facts behind `remem reingest status`. Read-only.

    `project=None` lists every designated project, as the command always
    has. The on-disk check runs only for `current_project`, against
    `root`: designations belong to a project and store no working
    directory (016_ingest_designations.sql), so the root is only known
    for the project this process is running inside. Every other project
    gets `checked_against=None`, which renders as "not checked" - a
    negative verdict has to name the ground it covered.

    `ingest_manual` writes a run row for whatever project it is given
    whether or not that project is designated (see its own docstring), so
    a plain `remem ingest` inside a repository that has never designated
    anything still leaves a fact worth showing. Without this, that run
    would be invisible here and `render_status` would say "not
    designated" even though the last thing this project did was ingest
    successfully - true of the designation, misleading about the project.
    Only `current_project` gets this treatment: it is the only project
    this call has a root for, and every other undesignated project is one
    this process cannot distinguish from one that was simply never
    touched.
    """
    by_project: dict[str, list[IngestDesignation]] = {}
    for d in store.ingest_designations(owner_id, project):
        by_project.setdefault(d.project, []).append(d)

    found: list[ProjectIngestStatus] = []
    for name, designations in by_project.items():
        checked_against = None
        missing: list[str] = []
        if root is not None and name == current_project:
            checked_against = root
            for d in designations:
                for p in d.paths:
                    if not _exists(root / p):
                        missing.append(p)
        found.append(
            ProjectIngestStatus(
                project=name,
                designations=designations,
                last_run=store.latest_ingest_run(owner_id, name),
                checked_against=checked_against,
                missing=missing,
            )
        )

    if (
        current_project is not None
        and current_project not in by_project
        and (project is None or project == current_project)
    ):
        last_run = store.latest_ingest_run(owner_id, current_project)
        if last_run is not None:
            # checked_against stays None even though `root` is known: there
            # is nothing designated, so there is nothing to check paths
            # against. `render_status` reads an empty `designations` list as
            # the signal to skip the check line entirely - "all designated
            # paths present" would otherwise describe a check that never ran.
            found.append(
                ProjectIngestStatus(
                    project=current_project,
                    designations=[],
                    last_run=last_run,
                    checked_against=None,
                    missing=[],
                )
            )
    return found


def _exists(path: Path) -> bool:
    """`Path.exists`, with an unreadable path reading as missing rather
    than as a crash - this is a status command."""
    try:
        return path.exists()
    except OSError:
        return False


def _when(at: datetime | None) -> str:
    return at.astimezone().strftime("%Y-%m-%d %H:%M") if at else "?"


def _run_lines(run: IngestRun | None) -> list[str]:
    """The last-run block. Four states, four spellings - each calls for a
    different action, so none may be mistaken for another."""
    if run is None:
        return ["last run: never. A session start inside this repository spawns one."]
    if run.finished_at is None:
        return [
            f"last run: {run.trigger}, started {_when(run.started_at)}, did not finish"
        ]
    lines = [
        f"last run: {run.trigger}, {_when(run.started_at)}, "
        f"{run.created} new, {run.changed} changed, {run.unchanged} unchanged, "
        f"{run.swept} swept, {run.embedded} embedded"
    ]
    for f in run.failures:
        lines.append(f"  failed: {f['path']}: {f['reason']}")
    for t in run.twins:
        lines.append(
            f"  twin: {t['path']} is new, but src:{t['existing']} has "
            f"{t['live']} live chunks"
        )
    if run.embed_error:
        lines.append(f"  embed skipped: {run.embed_error}")
    return lines


def render_status(found: list[ProjectIngestStatus], project: str | None) -> str:
    """Human-readable `remem reingest status`. `status_to_dict` is the
    `--json` half; both read the same objects so they cannot disagree."""
    if not found:
        return (
            f"{project or 'This project'} is not designated for automatic "
            f"re-ingest. Designate it with `remem reingest designate "
            f"<paths>`."
        )
    lines: list[str] = []
    for s in found:
        if not s.designations:
            # The `status()` fallback for a manually-ingested, undesignated
            # project: there is no designation line to print, and - since
            # nothing was designated - no disk check to report either way.
            # Printing "all present" or "not checked" here would describe a
            # check that never happened; the not-designated sentence is the
            # true statement, and the run line after it still says what did.
            lines.append(
                f"{s.project} is not designated for automatic re-ingest. "
                f"Designate it with `remem reingest designate <paths>`."
            )
            lines.extend(_run_lines(s.last_run))
            continue
        for d in s.designations:
            half = "archive" if d.archive else "default"
            lines.append(f"{s.project} ({half}): {', '.join(d.paths)}")
        lines.extend(_run_lines(s.last_run))
        if s.checked_against is None:
            lines.append(f"paths not checked: run from inside {s.project}'s repository")
        elif s.missing:
            lines.append(
                f"missing on disk: {', '.join(s.missing)}  "
                f"(checked against {s.checked_against})"
            )
        else:
            lines.append(
                f"all designated paths present  (checked against {s.checked_against})"
            )
    return "\n".join(lines)


def _run_to_dict(run: IngestRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "id": str(run.id),
        "trigger": str(run.trigger),
        "archive": run.archive,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created": run.created,
        "changed": run.changed,
        "unchanged": run.unchanged,
        "swept": run.swept,
        "embedded": run.embedded,
        "failures": list(run.failures),
        "twins": list(run.twins),
        "embed_error": run.embed_error,
    }


def status_to_dict(found: list[ProjectIngestStatus]) -> list[dict]:
    return [
        {
            "project": s.project,
            "designations": [
                {"archive": d.archive, "paths": list(d.paths)} for d in s.designations
            ],
            "last_run": _run_to_dict(s.last_run),
            "check": {
                "checked_against": (
                    str(s.checked_against) if s.checked_against else None
                ),
                "missing": list(s.missing),
            },
        }
        for s in found
    ]


def advisories(
    store: Store,
    owner_id: UUID,
    *,
    current_project: str | None,
    root: Path | None,
) -> list[str]:
    """One line per designated project whose last run is unhealthy, for
    `remem record status` - the fail-loud half of a fail-soft pipeline,
    which already carries the doctor advisory the same way.

    Unhealthy is: the latest run finished with failures, or started and
    never finished, or (for the current project only, the one with a root
    to check against) a designated path is missing on disk. Every line
    ends with STATUS_POINTER, scope included.
    """
    lines: list[str] = []
    for s in status(store, owner_id, None, current_project=current_project, root=root):
        # status()'s fallback surfaces an undesignated current project's
        # manual run too - right for the status screen, which is answering
        # "what happened here", but the spec scopes this advisory to
        # designated projects. `remem ingest` is already fail-loud about its
        # own failures, and an undesignated project's run row never repairs
        # itself, so repeating it here would be a permanently stuck line
        # whose pointer (remem reingest status --project X) contradicts
        # itself from outside this directory - "not designated", no run
        # shown. The fallback's rows are the only ones with an empty
        # designations list, which is what makes them identifiable here.
        if not s.designations:
            continue
        pointer = STATUS_POINTER.format(project=s.project)
        run = s.last_run
        if run is not None and run.finished_at is None:
            lines.append(
                f"{s.project}: {run.trigger} ingest started "
                f"{_when(run.started_at)} and did not finish - {pointer}"
            )
        elif run is not None and run.failures:
            lines.append(
                f"{s.project}: last {run.trigger} ingest "
                f"({_when(run.started_at)}) had {len(run.failures)} "
                f"failure(s) - {pointer}"
            )
        if s.missing:
            lines.append(
                f"{s.project}: designated path(s) missing on disk: "
                f"{', '.join(s.missing)} - {pointer}"
            )
    return lines
