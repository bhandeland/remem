"""Knowledge bases: resolving membership and rendering context blocks."""

from __future__ import annotations

from uuid import UUID

from remem.domain import Collection, CollectionQuery, Entry, Kind, Query, new_id
from remem.services.write import EntryNotFound
from remem.store import Store

RESOLVE_LIMIT = 200

__all__ = [
    "CollectionNotFound",
    "EntryNotFound",
    "RulesExceedBudget",
    "create",
    "get",
    "pin",
    "render",
    "resolve",
]


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


def pin(
    store: Store,
    owner_id: UUID,
    slug: str,
    entry_id: UUID,
    position: int = 0,
) -> None:
    """Pin an entry into a knowledge base so it is always included.

    Both the knowledge base and the entry must belong to this owner. Pinning
    something the owner cannot see would write a member row that
    `pinned_entries` correctly refuses to render - a pin that appears to
    succeed and then silently never shows up.
    """
    collection = get(store, owner_id, slug)
    if store.get_entry(entry_id, owner_id) is None:
        raise EntryNotFound(str(entry_id))
    store.pin(collection.id, entry_id, position, owner_id)


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


class RulesExceedBudget(Exception):
    """Rules alone do not fit the character budget.

    Raised rather than truncating: an agent given a partial rule proceeds
    believing it has the conventions, which is worse than having none.
    """


def _render_entry(entry: Entry) -> str:
    tags = ", ".join(entry.tags)
    meta = f"_id: {entry.id}_" + (f" _tags: {tags}_" if tags else "")
    return f"### {entry.title}\n\n{entry.body}\n\n{meta}\n"


def render(collection: Collection, entries: list[Entry], max_chars: int) -> str:
    """Render a knowledge base as a context block.

    Rules first and never truncated; then other entries, whole ones only,
    until the budget runs out; then an explicit count of what was dropped.
    """
    header = f"# {collection.title}\n"
    if collection.description:
        header += f"\n{collection.description}\n"

    rules = [e for e in entries if e.kind == Kind.RULE]
    others = [e for e in entries if e.kind != Kind.RULE]

    parts = [header]
    if rules:
        parts.append("\n## Rules\n")
        parts.extend(_render_entry(e) for e in rules)

    used = sum(len(p) for p in parts)
    if used > max_chars:
        raise RulesExceedBudget(
            f"rules and header need {used} chars, budget is {max_chars}; "
            "prune the knowledge base or raise the budget"
        )

    included = 0
    body_parts: list[str] = []
    for e in others:
        chunk = _render_entry(e)
        if used + len(chunk) > max_chars:
            break
        body_parts.append(chunk)
        used += len(chunk)
        included += 1

    if body_parts:
        parts.append("\n## Knowledge\n")
        parts.extend(body_parts)

    omitted = len(others) - included
    if omitted:
        # Never truncate silently: a shortened block reads to an agent as
        # the complete picture.
        parts.append(
            f"\n- {omitted} more entries not shown "
            f"(remem kb show {collection.slug} --full)\n"
        )

    return "".join(parts)
