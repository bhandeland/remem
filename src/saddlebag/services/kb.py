"""Knowledge bases: resolving membership and rendering context blocks."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from saddlebag.domain import (
    INJECTED_ORIGINS,
    Collection,
    CollectionQuery,
    Entry,
    Kind,
    Query,
    new_id,
)
from saddlebag.services.write import EntryNotFound
from saddlebag.store import Store

RESOLVE_LIMIT = 200

#: The fraction of the budget at which `budget_advisories` starts warning.
#:
#: The hard failure is worth reporting, but by the time it fires injection
#: has already been dead in every session since the rule that tipped it over
#: was written - and nothing said so, because every injection path is
#: fail-soft. The value of this advisory is almost entirely in the warning
#: that comes before it, so there is one. 0.8 is a judgement, not a
#: measurement: it is far enough back that a single ordinary rule (a title
#: and a one-line summary, a few hundred characters against a default
#: budget in the tens of thousands) cannot cross the whole gap from silent
#: to dead, and close enough that a knowledge base which has simply been
#: small all along never mentions itself.
BUDGET_WARN_FRACTION = 0.8

#: Every advisory line ends with this, as ingest's and memory's do. `kb
#: show --full` is the command that names which rules are costing what,
#: which is the thing a person has to see before they can prune one.
BUDGET_POINTER = "see: bag kb show {slug} --full"

__all__ = [
    "BUDGET_POINTER",
    "BUDGET_WARN_FRACTION",
    "CollectionNotFound",
    "EntryNotFound",
    "RulesExceedBudget",
    "advisories",
    "budget_advisories",
    "create",
    "get",
    "pin",
    "render",
    "resolve",
    "rules_chars",
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
            "automatically, or pin entries into it with `bag kb pin`."
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
        raise RuntimeError(f"failed to pin {entry_id} into {slug}")


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
    already written as directives ("Run bag ingest from the repository
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


def _header_and_rules(collection: Collection, entries: list[Entry]) -> list[str]:
    """The parts of a block that are never dropped, in order.

    Factored out of `render` so that `rules_chars` - and therefore
    `budget_advisories` - measures the literal same characters the raise
    site counts, rather than a second implementation that agrees with it
    today. Two copies of this arithmetic drifting apart would mean the
    advisory reporting healthy on a knowledge base that is raising, which
    is the one failure the advisory exists to make impossible.
    """
    header = f"# {collection.title}\n"
    if collection.description:
        header += f"\n{collection.description}\n"

    rules = [e for e in entries if e.kind == Kind.RULE]
    parts = [header]
    if rules:
        parts.append("\n## Rules\n")
        parts.extend(_render_entry(e) for e in rules)
    return parts


def rules_chars(collection: Collection, entries: list[Entry]) -> int:
    """What this knowledge base spends of the budget before it spends any
    of it on notes: the header plus every rule, rendered.

    This, and not the length of the rendered block, is the number that
    predicts the failure. Rules never truncate and notes are dropped whole
    to make room, so the block that ships is capped at the budget by
    construction and can never measure over it - a length-based check
    reports healthy on a knowledge base that is silently losing every note
    it has, and goes on reporting healthy right up to the moment injection
    dies. Only this quantity moves, and only pruning rules moves it back.
    """
    return sum(len(p) for p in _header_and_rules(collection, entries))


def budget_advisories(
    store: Store,
    owner_id: UUID,
    max_chars: int,
    *,
    warn_fraction: float = BUDGET_WARN_FRACTION,
) -> list[str]:
    """One line per knowledge base whose rules are crowding the budget.

    For `bag record status`, the fail-loud half of a fail-soft pipeline,
    which already carries the doctor, ingest and memory advisories the same
    way. This is the only place the failure can be told: `RulesExceedBudget`
    is raised inside `render`, and every caller of `render` that matters is
    a hook which by hard contract exits 0 and prints nothing - so when a
    knowledge base outgrows the budget, context injection dies on Claude
    Code, opencode and Cursor at once, with no output anywhere. It has
    happened in this repository.

    Deliberately not in `bag doctor`: doctor reads files and opens no
    database, so that a diagnostic still works when the system does not,
    and this question cannot be answered without resolving a collection.

    Two tiers, both rendered the same way, because a reader scanning
    `record status` needs the difference in the sentence rather than in a
    field: over the budget says injection is already dead, near it says it
    is about to be.
    """
    lines: list[str] = []
    for collection in store.list_collections(owner_id):
        entries = resolve(store, owner_id, collection.slug)
        used = rules_chars(collection, entries)
        pointer = BUDGET_POINTER.format(slug=collection.slug)
        if used > max_chars:
            lines.append(
                f"knowledge base '{collection.slug}': its rules and header "
                f"need {used} chars against a {max_chars} budget, so its "
                f"context block is not being injected in any session - "
                f"prune rules from it or raise BAG_MAX_CHARS - {pointer}"
            )
        elif used >= max_chars * warn_fraction:
            lines.append(
                f"knowledge base '{collection.slug}': its rules and header "
                f"use {used} of a {max_chars} budget, and once they pass it "
                f"context injection stops silently in every session - prune "
                f"rules from it or raise BAG_MAX_CHARS - {pointer}"
            )
    return lines


@dataclass(frozen=True)
class Rendered:
    """A context block plus what it carries.

    The counts exist for the session banner, which reports what the model
    actually received. `rules` equals the number of rules resolved, since a
    rule is never dropped; `notes` is only the notes that fit, and
    `notes_dropped` the rest - counting resolved entries would claim notes
    the block never carried.
    """

    text: str
    rules: int
    notes: int
    notes_dropped: int


def render(collection: Collection, entries: list[Entry], max_chars: int) -> str:
    """Render a knowledge base as a context block.

    Rules first and never truncated; then other entries, whole ones only,
    until the budget runs out; then an explicit count of what was dropped.
    """
    return render_block(collection, entries, max_chars).text


def render_block(
    collection: Collection, entries: list[Entry], max_chars: int
) -> Rendered:
    """`render`, plus the counts. See `Rendered` for why they exist."""
    others = [e for e in entries if e.kind != Kind.RULE]

    parts = _header_and_rules(collection, entries)
    used = sum(len(p) for p in parts)
    if used > max_chars:
        raise RulesExceedBudget(
            f"rules and header need {used} chars, budget is {max_chars}; "
            "prune the knowledge base or raise the budget"
        )

    def _notice(count: int) -> str:
        return (
            f"\n- {count} more entries not shown "
            f"(bag kb show {collection.slug} --full)\n"
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
    while (
        included < len(others)
        and used + len(_notice(len(others) - included)) > max_chars
    ):
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

    return Rendered(
        text="".join(parts),
        rules=len(entries) - len(others),
        notes=included,
        notes_dropped=omitted,
    )
