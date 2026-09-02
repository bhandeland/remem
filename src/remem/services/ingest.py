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

from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from remem.domain import Entry, Kind, Origin, Query
from remem.markdown import Chunk, split
from remem.services.write import remember, supersede
from remem.store import Store

#: The sweep lists a file's live chunks through `Query.limit`. Sweeping a
#: silently truncated list would supersede live chunks at random, which is
#: the worst failure available here - so exceeding this raises instead.
MAX_CHUNKS_PER_FILE = 200


class TooManyChunks(Exception):
    """A document produced more chunks than one sweep can safely list."""


@dataclass(slots=True)
class Report:
    created: int = 0
    changed: int = 0
    unchanged: int = 0
    swept: int = 0
    failures: list[tuple[Path, str]] = field(default_factory=list)

    def merge(self, other: Report) -> None:
        self.created += other.created
        self.changed += other.changed
        self.unchanged += other.unchanged
        self.swept += other.swept
        self.failures.extend(other.failures)


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


def ingest_file(
    store: Store,
    owner_id: UUID,
    path: Path,
    *,
    project: str | None,
    archive: bool = False,
    dry_run: bool = False,
) -> Report:
    """Ingest one file."""
    path = Path(path)
    chunks = split(path.read_text(), doc_title=path.stem)
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
        elif current.body == chunk.body:
            # Skip entirely rather than rewrite an identical row: an update
            # would churn updated_at and make `remem embed` look like it has
            # work to redo when the stored vector is still correct.
            report.unchanged += 1
            if chunk.anchor:
                anchor_id = current.id
        else:
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
    for path in _discover(paths):
        try:
            report.merge(
                ingest_file(store, owner_id, path, project=project,
                            archive=archive, dry_run=dry_run)
            )
        except (OSError, UnicodeDecodeError, TooManyChunks) as exc:
            report.failures.append((path, str(exc)))
    return report


def _discover(paths: list[Path]) -> list[Path]:
    """Files as given, directories globbed for **/*.md, sorted for a stable
    report. Sorted matters: a dry run the user reads and then re-runs for
    real must list its files in the same order both times."""
    found: list[Path] = []
    for path in paths:
        path = Path(path)
        found.extend(sorted(path.rglob("*.md")) if path.is_dir() else [path])
    return found
