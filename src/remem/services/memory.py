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
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from remem.services import kb
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
