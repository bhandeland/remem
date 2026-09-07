"""Knowledge bases: resolving membership and rendering context blocks."""

from __future__ import annotations

from uuid import UUID

from remem.domain import (
    Collection,
    CollectionQuery,
    Entry,
    INJECTED_ORIGINS,
    Kind,
    Query,
    new_id,
)
from remem.services.write import EntryNotFound
from remem.store import Store

RESOLVE_LIMIT = 200

__all__ = [
    "CollectionNotFound",
    "EntryNotFound",
    "RulesExceedBudget",
    "advisories",
    "create",
    "get",
    "pin",
    "render",
    "resolve",
    "set_query",
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


def advisories(collection: Collection) -> list[str]:
    """Things worth telling the user about a knowledge base they just made.

    A knowledge base with an empty query matches nothing, forever. Saying so
    at creation is the only moment the user is looking.
    """
    if collection.query.is_empty():
        return [
            f"knowledge base '{collection.slug}' has no query, so it will "
            "match no entries. Pass --project or --tag to select entries "
            "automatically, or pin entries into it with `remem kb pin`."
        ]
    return []


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
    if not store.pin(collection.id, entry_id, position, owner_id):
        # `get` and `get_entry` above already proved both rows exist under
        # this owner, so the store's guards should be satisfied by the time
        # we get here. A False means something changed in between - surface
        # it rather than reporting a pin that wrote no row, exactly as
        # `write.supersede` does for `set_superseded`.
        raise RuntimeError(
            f"failed to pin {entry_id} into {slug}"
        )


def set_query(
    store: Store, owner_id: UUID, slug: str, query: CollectionQuery
) -> Collection:
    """Replace a knowledge base's query.

    Without this a query is fixed at creation: a knowledge base made with no
    --project or --tag matches nothing forever, and re-running `kb new` with
    the same slug is an undocumented upsert rather than a repair.
    Title, description, and pinned members are untouched.
    """
    collection = get(store, owner_id, slug)
    collection.query = query
    return store.put_collection(collection)


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
                # Machine-written entries stay out of the block that loads into
                # every session. Pinning is the deliberate way to promote one.
                origins=list(INJECTED_ORIGINS),
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
    return f"### {entry.title}\n\n{_content(entry)}{meta}\n"


def _content(entry: Entry) -> str:
    """What an entry contributes to the block, above its id line.

    A rule contributes its SUMMARY, not its body. Rule bodies here are
    essays - the incident that produced the rule, the reasoning, the
    lesson - and every session was paying for case history that nothing
    reads unless someone asks why. The body is one `recall` away, and the
    id line above is how to reach it.

    A rule with no summary contributes nothing but its title. That is the
    deliberate floor rather than a fallback to the body: the rules written
    before the summary requirement must keep rendering, and rendering
    their bodies is the failure this change exists to fix. Titles here are
    already written as directives ("Run remem ingest from the repository
    root, never a subdirectory"), so a title alone still instructs.

    Deriving a short form from the body was measured and rejected: the
    first paragraph of a rule is the incident, not the instruction.

    Every other kind renders its body unchanged. Notes and docs are not
    injected into every session, so their cost was never the problem.
    """
    if entry.kind is not Kind.RULE:
        return f"{entry.body}\n\n"
    if entry.summary:
        return f"{entry.summary}\n\n"
    return ""


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

    def _notice(count: int) -> str:
        return (
            f"\n- {count} more entries not shown "
            f"(remem kb show {collection.slug} --full)\n"
        )

    heading = "\n## Knowledge\n"
    included = 0
    body_parts: list[str] = []
    for e in others:
        chunk = _render_entry(e)
        extra = len(chunk) + (len(heading) if not body_parts else 0)
        if used + extra > max_chars:
            break
        body_parts.append(chunk)
        used += extra
        included += 1

    # The notice is part of the block, so it has to fit inside the budget too.
    # Drop further entries until it does, rather than overshooting by its length.
    while included < len(others) and used + len(_notice(len(others) - included)) > max_chars:
        if not body_parts:
            break
        used -= len(body_parts.pop())
        included -= 1
        if not body_parts:
            used -= len(heading)

    if body_parts:
        parts.append(heading)
        parts.extend(body_parts)

    omitted = len(others) - included
    if omitted:
        # Never truncate silently: a shortened block reads to an agent as
        # the complete picture.
        parts.append(_notice(omitted))

    return "".join(parts)
