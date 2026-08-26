"""Knowledge bases: resolving membership and rendering context blocks."""

from __future__ import annotations

from uuid import UUID

from remem.domain import Collection, CollectionQuery, Entry, Query, new_id
from remem.store import Store

RESOLVE_LIMIT = 200


class CollectionNotFound(Exception):
    """Raised when a collection slug does not exist for this owner."""


def create(
    store: Store,
    owner_id: UUID,
    *,
    slug: str,
    title: str,
    description: str | None = None,
    project: str | None = None,
    query: CollectionQuery | None = None,
) -> Collection:
    return store.put_collection(
        Collection(
            id=new_id(),
            slug=slug,
            title=title,
            owner_id=owner_id,
            description=description,
            project=project,
            query=query or CollectionQuery(),
        )
    )


def get(store: Store, owner_id: UUID, slug: str) -> Collection:
    collection = store.get_collection(slug, owner_id)
    if collection is None:
        raise CollectionNotFound(slug)
    return collection


def resolve(store: Store, owner_id: UUID, slug: str) -> list[Entry]:
    """Pinned members first, then query matches. Deduped, pinned wins."""
    collection = get(store, owner_id, slug)

    entries = list(store.pinned_entries(collection.id, owner_id))
    seen = {e.id for e in entries}

    if not collection.query.is_empty():
        hits = store.search(
            Query(
                kinds=list(collection.query.kinds),
                project=collection.query.project,
                tags=list(collection.query.tags),
                limit=RESOLVE_LIMIT,
            ),
            owner_id,
        )
        for hit in hits:
            if hit.entry.id not in seen:
                seen.add(hit.entry.id)
                entries.append(hit.entry)

    # A pinned entry that was later superseded should not resurface.
    return [e for e in entries if e.superseded_by is None]
