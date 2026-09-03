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
