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
from pathlib import Path, PurePosixPath
from uuid import UUID

from remem.domain import Entry, IngestDesignation, Kind, Origin, Query
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
            raise BadDesignation(
                "No paths given. To clear a designation, pass None."
            )
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
            return tag[len("sec:"):]
    return ""


def _src_of(entry: Entry) -> str:
    """An entry's `src:` path, or "" when it has none."""
    for tag in entry.tags:
        if tag.startswith("src:"):
            return tag[len("src:"):]
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
            live = len(_live_chunks(store, owner_id, Path(src)))
            report.twins.append((path.as_posix(), src, live))

    for chunk in chunks:
        seen.add(chunk.slug)
        current = existing.get(chunk.slug)
        if current is None:
            report.created += 1
            if not dry_run:
                written = remember(
                    store, owner_id,
                    title=chunk.title, body=chunk.body, kind=Kind.DOC,
                    project=project, origin=origin,
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
                    store, owner_id, current.id,
                    title=chunk.title, body=chunk.body,
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
            report.merge(
                ingest_file(store, owner_id, path, project=project,
                            root=root, archive=archive, dry_run=dry_run)
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
        found.extend(
            [h.relative_to(root) for h in hits] if root is not None else hits
        )
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
    read and no embedder is built.

    Paths are resolved against `root` (the git root) rather than the process
    working directory. The spawned job inherits whatever directory the
    harness happened to be in, which is not something a designation made
    weeks earlier can know.

    Nothing here raises. Every caller is automatic, and a knowledge tool
    must never be why a session start goes wrong - a path that vanished
    lands in `report.failures` and an absent embedder in `embed_error`,
    both for a debug channel to print and neither for anyone to trip over.
    """
    result = RefreshResult()
    designated = store.ingest_designations(owner_id, project)
    if not designated:
        return result

    for designation in designated:
        result.report.merge(
            ingest_paths(
                store, owner_id,
                [Path(p) for p in designation.paths],
                project=project,
                root=root,
                archive=designation.archive,
            )
        )

    try:
        embedded = backfill_if_pending(
            store, owner_id, embed_model, load_embedder
        )
    except Exception as exc:
        # Losing the semantic tier is worth strictly less than the entries
        # just written, and `remem embed` remains the loud way to find out
        # that the embedder is broken.
        result.embed_error = str(exc)
    else:
        result.embedded = embedded.embedded if embedded else 0
    return result
