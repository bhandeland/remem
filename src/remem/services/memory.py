"""Claude Code's file-based memory directory, as a generated view of a
designated collection.

Every policy decision lives here: which collection is exported, what a
conflict is, and which of the six cases a given file falls into. The CLI
resolves a working directory and a project and prints counts.

The directory has a second writer that cannot be told to stop - Claude Code
writes memories there unprompted, mid-session - so the sync adopts before it
regenerates. See docs/superpowers/specs/2026-09-02-claude-memory-design.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from remem import memory_file
from remem.domain import Entry, Kind, Origin
from remem.services import kb
from remem.services.write import remember, supersede
from remem.store import Store


class NotDesignated(Exception):
    """This project has no memory collection, so nothing is exported.

    Not an error state to repair - it is the default, and the opt-in gate.
    Raised only by callers that were asked to sync a specific project.
    """


def designate(
    store: Store, owner_id: UUID, project: str, slug: str | None
) -> None:
    """Point a project's memory export at a collection, or clear it.

    The collection must already exist. Creating one here would give it an
    empty CollectionQuery, and an empty query matches nothing forever - so
    the friendly version of this command would silently guarantee that no
    memory is ever exported. `kb create` says so through kb.advisories();
    this does not get to bypass it.
    """
    if slug is not None:
        kb.get(store, owner_id, slug)  # raises CollectionNotFound
    store.set_memory_collection(owner_id, project, slug)


def designation(store: Store, owner_id: UUID, project: str) -> str | None:
    return store.memory_collection(owner_id, project)


#: Dot-prefixed so Claude Code does not index it as a memory.
WATERMARK_NAME = ".remem-sync.json"


class Case(StrEnum):
    ADOPT_NEW = "adopt_new"
    HEAL = "heal"
    ADOPT_EDIT = "adopt_edit"
    REGENERATE = "regenerate"
    CONFLICT = "conflict"
    DELETE = "delete"
    UNCHANGED = "unchanged"


@dataclass(slots=True, frozen=True)
class Watermark:
    entry_id: str
    body_sha: str
    #: Never compared. Shown by `remem memory status` so a stale directory is
    #: visible; comparing it would reintroduce the clocks this whole scheme
    #: exists to avoid.
    exported_at: str


def classify(
    *,
    file_sha: str | None,
    entry_sha: str | None,
    mark: Watermark | None,
) -> Case:
    """Which of the six cases this name falls into.

    Pure, and separated from the sync deliberately: this is the part with all
    the combinations, and a pure function means every one of them is tested
    on CI without a database.

    Body comparison alone cannot answer "which side moved" - it says the two
    differ, not who changed. The watermark is what makes it answerable.
    """
    if file_sha is None and entry_sha is None:
        return Case.UNCHANGED  # nothing on either side; nothing to do
    if file_sha is None:
        return Case.REGENERATE  # entry with no file - write it
    if entry_sha is None:
        # No entry. Delete only what we wrote and know to be untouched;
        # anything else is an edit worth keeping.
        if mark is not None and mark.body_sha == file_sha:
            return Case.DELETE
        return Case.ADOPT_EDIT if mark is not None else Case.ADOPT_NEW
    if file_sha == entry_sha:
        # Agreement. Either it never moved, or both sides moved to the same
        # text - which is agreement too, not a conflict worth a user's time.
        if mark is not None and mark.body_sha == file_sha:
            return Case.UNCHANGED
        return Case.HEAL
    if mark is None:
        # They differ and there is no watermark, so nothing says which moved.
        return Case.CONFLICT
    file_moved = mark.body_sha != file_sha
    entry_moved = mark.body_sha != entry_sha
    if file_moved and entry_moved:
        return Case.CONFLICT
    return Case.ADOPT_EDIT if file_moved else Case.REGENERATE


def load_watermarks(directory: Path) -> dict[str, Watermark]:
    """Missing or unreadable is an empty mapping, not an error.

    Degrading here is safe because of how classify() treats a missing mark:
    a file that still matches its entry heals, and one that differs becomes a
    conflict a human resolves. Losing this file costs attention, never data.
    """
    path = directory / WATERMARK_NAME
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Watermark] = {}
    for name, row in raw.items():
        try:
            out[name] = Watermark(
                entry_id=row["entry_id"],
                body_sha=row["body_sha"],
                exported_at=row["exported_at"],
            )
        except (TypeError, KeyError):
            continue
    return out


def save_watermarks(directory: Path, marks: dict[str, Watermark]) -> None:
    path = directory / WATERMARK_NAME
    payload = {
        name: {
            "entry_id": m.entry_id,
            "body_sha": m.body_sha,
            "exported_at": m.exported_at,
        }
        for name, m in sorted(marks.items())
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


#: Identity. One file, one entry, one tag - following handoff's `topic:` and
#: ingest's `src:`/`sec:`. The slug survives a rename of the file itself and
#: is what `[[wiki-links]]` resolve against.
MEM_TAG_PREFIX = "mem:"

#: Written beside a memory when both sides moved, so the store's version is
#: not lost while the file keeps the user's edit. The suffix is excluded from
#: the directory scan: a sidecar is remem's report of a conflict, not a
#: memory, and adopting one would mint a second entry from the same knowledge
#: and then regenerate a file for it on every sync thereafter.
CONFLICT_SUFFIX = ".remem-conflict.md"


@dataclass(slots=True)
class Report:
    adopted: int = 0
    healed: int = 0
    edited: int = 0
    regenerated: int = 0
    deleted: int = 0
    unchanged: int = 0
    conflicts: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)


def _name_of(entry: Entry) -> str | None:
    for tag in entry.tags:
        if tag.startswith(MEM_TAG_PREFIX):
            return tag[len(MEM_TAG_PREFIX):]
    return None


def sync(
    store: Store,
    owner_id: UUID,
    *,
    project: str,
    directory: Path,
    dry_run: bool = False,
) -> Report:
    """Adopt what Claude wrote, then regenerate the directory from the store.

    Adoption comes first on purpose. The directory has a second writer that
    cannot be told to stop, so regenerating without adopting would destroy
    every memory written since the last run.
    """
    slug = designation(store, owner_id, project)
    if slug is None:
        raise NotDesignated(project)

    report = Report()
    marks = load_watermarks(directory)
    index: dict[str, str] = {}
    index_path = directory / "MEMORY.md"
    if index_path.exists():
        index = memory_file.parse_index(index_path.read_text())

    # --- read both sides ------------------------------------------------
    files: dict[str, memory_file.MemoryFile] = {}
    if directory.exists():
        for path in sorted(directory.glob("*.md")):
            if path.name == "MEMORY.md" or path.name.endswith(CONFLICT_SUFFIX):
                continue
            try:
                files[path.stem] = memory_file.parse(
                    path.read_text(),
                    name=path.stem,
                    title=index.get(path.name, ""),
                )
            except (memory_file.MalformedMemoryFile, OSError) as exc:
                # Collect rather than abort: one bad file must not cost the
                # other thirty-seven.
                report.failures.append((path.stem, str(exc)))

    entries = {
        name: e
        for e in kb.resolve(store, owner_id, slug)
        if (name := _name_of(e)) is not None
    }

    # --- classify and apply ---------------------------------------------
    now = datetime.now(timezone.utc).isoformat()
    for name in sorted(set(files) | set(entries)):
        mf = files.get(name)
        entry = entries.get(name)
        case = classify(
            file_sha=None if mf is None else memory_file.body_sha(mf.body),
            entry_sha=None if entry is None else memory_file.body_sha(entry.body),
            mark=marks.get(name),
        )

        if case is Case.CONFLICT:
            report.conflicts.append(name)
            if not dry_run and entry is not None:
                (directory / f"{name}{CONFLICT_SUFFIX}").write_text(
                    memory_file.render(_as_file(entry, name))
                )
            continue

        if case is Case.UNCHANGED:
            report.unchanged += 1
            continue

        if case is Case.ADOPT_NEW:
            report.adopted += 1
            if not dry_run:
                new = remember(
                    store, owner_id,
                    title=mf.title or memory_file.title_from_name(name),
                    body=mf.body,
                    summary=mf.description,
                    kind=Kind.NOTE,
                    project=project,
                    tags=_tags_for(mf, name),
                    origin=Origin.AGENT,
                )
                # Deliberately not pinned. kb.resolve returns pinned members
                # regardless of the collection's query, so pinning here would
                # make the query decorative and an entry could never leave the
                # collection - which is the one thing that deletes a file.
                # Pins stay the user's escape hatch, via `remem kb pin`.
                marks[name] = Watermark(
                    entry_id=str(new.id),
                    body_sha=memory_file.body_sha(mf.body),
                    exported_at=now,
                )
            continue

        if case is Case.ADOPT_EDIT:
            if entry is None:
                # The file was edited for an entry that has left the
                # collection. Re-adopting would duplicate the entry that
                # still exists outside it, and deleting would destroy the
                # edit, so this is a conflict: report it, touch nothing.
                report.conflicts.append(name)
                continue
            report.edited += 1
            if not dry_run:
                new = supersede(
                    store, owner_id, entry.id,
                    title=mf.title or entry.title,
                    body=mf.body,
                    summary=mf.description,
                )
                marks[name] = Watermark(
                    entry_id=str(new.id),
                    body_sha=memory_file.body_sha(mf.body),
                    exported_at=now,
                )
            continue

        if case is Case.HEAL:
            report.healed += 1
            if not dry_run:
                marks[name] = Watermark(
                    entry_id=str(entry.id),
                    body_sha=memory_file.body_sha(entry.body),
                    exported_at=now,
                )
            continue

        if case is Case.REGENERATE:
            report.regenerated += 1
            if not dry_run:
                directory.mkdir(parents=True, exist_ok=True)
                # An Entry has nowhere to store the metadata keys remem does
                # not own, so they are read back off the file being replaced.
                # A file remem creates from scratch simply has none.
                (directory / f"{name}.md").write_text(
                    memory_file.render(
                        _as_file(entry, name, extra=None if mf is None else mf.extra)
                    )
                )
                marks[name] = Watermark(
                    entry_id=str(entry.id),
                    body_sha=memory_file.body_sha(entry.body),
                    exported_at=now,
                )
            continue

        if case is Case.DELETE:
            report.deleted += 1
            if not dry_run:
                (directory / f"{name}.md").unlink(missing_ok=True)
                marks.pop(name, None)

    if not dry_run:
        directory.mkdir(parents=True, exist_ok=True)
        # Re-read from the store rather than reusing `entries`, so the index
        # includes anything adopted in this run.
        live = [
            _as_file(e, n)
            for e in kb.resolve(store, owner_id, slug)
            if (n := _name_of(e)) is not None
        ]
        index_path.write_text(memory_file.render_index(live))
        save_watermarks(directory, marks)

    return report


def _tags_for(mf: memory_file.MemoryFile, name: str) -> list[str]:
    tags = [f"{MEM_TAG_PREFIX}{name}"]
    if mf.type:
        tags.append(f"type:{mf.type}")
    return tags


def _as_file(
    entry: Entry, name: str, extra: dict[str, str] | None = None
) -> memory_file.MemoryFile:
    type_ = None
    for tag in entry.tags:
        if tag.startswith("type:"):
            type_ = tag[len("type:"):]
    return memory_file.MemoryFile(
        name=name,
        title=entry.title,
        description=entry.summary or "",
        type=type_,
        body=entry.body,
        extra=dict(extra or {}),
    )
