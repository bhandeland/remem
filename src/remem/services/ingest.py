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

    for chunk in chunks:
        current = existing.get(chunk.slug)
        if current is None:
            report.created += 1
            if not dry_run:
                remember(
                    store, owner_id,
                    title=chunk.title, body=chunk.body, kind=Kind.DOC,
                    project=project, origin=origin,
                    tags=_tags_for(path, chunk),
                )
        elif current.body == chunk.body:
            # Skip entirely rather than rewrite an identical row: an update
            # would churn updated_at and make `remem embed` look like it has
            # work to redo when the stored vector is still correct.
            report.unchanged += 1
        else:
            report.changed += 1
            if not dry_run:
                supersede(store, owner_id, current.id,
                          title=chunk.title, body=chunk.body)
    return report
