"""Every decision about writing knowledge lives here."""

from __future__ import annotations

from uuid import UUID

from remem.domain import Entry, Kind, Origin, new_id
from remem.store import Store


class EntryNotFound(Exception):
    """Raised when an entry id does not exist for this owner."""


def remember(
    store: Store,
    owner_id: UUID,
    *,
    title: str,
    body: str,
    kind: Kind = Kind.MEMORY,
    project: str | None = None,
    tags: list[str] | None = None,
    links: list[UUID] | None = None,
    agent: str | None = None,
    session_id: str | None = None,
    origin: Origin = Origin.AGENT,
) -> Entry:
    entry = Entry(
        id=new_id(),
        kind=kind,
        title=title,
        body=body,
        owner_id=owner_id,
        project=project,
        tags=list(tags or []),
        links=list(links or []),
        agent=agent,
        session_id=session_id,
        origin=origin,
    )
    return store.put_entry(entry)


def _require(store: Store, owner_id: UUID, entry_id: UUID) -> Entry:
    entry = store.get_entry(entry_id, owner_id)
    if entry is None:
        raise EntryNotFound(str(entry_id))
    return entry


def update(
    store: Store,
    owner_id: UUID,
    entry_id: UUID,
    *,
    title: str | None = None,
    body: str | None = None,
    tags: list[str] | None = None,
    project: str | None = None,
) -> Entry:
    entry = _require(store, owner_id, entry_id)
    if title is not None:
        entry.title = title
    if body is not None:
        entry.body = body
    if tags is not None:
        entry.tags = list(tags)
    if project is not None:
        entry.project = project
    return store.put_entry(entry)


def supersede(
    store: Store,
    owner_id: UUID,
    entry_id: UUID,
    *,
    title: str,
    body: str,
) -> Entry:
    """Replace knowledge that stopped being true. The old entry is kept."""
    old = _require(store, owner_id, entry_id)
    replacement = remember(
        store,
        owner_id,
        title=title,
        body=body,
        kind=old.kind,
        project=old.project,
        tags=list(old.tags),
        agent=old.agent,
        origin=old.origin,
    )
    ok = store.set_superseded(old.id, replacement.id, owner_id)
    if not ok:
        # `remember` above just created `replacement` with this same
        # `owner_id`, and `old` was fetched via `_require` under that same
        # owner, so `set_superseded`'s existence-and-ownership check should
        # always be satisfied here. If it isn't, something changed between
        # our read and write (e.g. concurrent modification) - surface that
        # loudly rather than returning a replacement that silently failed
        # to supersede anything.
        raise RuntimeError(
            f"failed to mark {old.id} superseded by {replacement.id}"
        )
    return replacement


def link(store: Store, owner_id: UUID, a_id: UUID, b_id: UUID) -> None:
    """Link two entries in both directions, without duplicating."""
    a = _require(store, owner_id, a_id)
    b = _require(store, owner_id, b_id)
    if b.id not in a.links:
        a.links.append(b.id)
        store.put_entry(a)
    if a.id not in b.links:
        b.links.append(a.id)
        store.put_entry(b)
