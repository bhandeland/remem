"""Every decision about writing knowledge lives here."""

from __future__ import annotations

from uuid import UUID

from remem.domain import INJECTED_ORIGINS, Entry, Kind, Origin, new_id
from remem.store import Store


class EntryNotFound(Exception):
    """Raised when an entry id does not exist for this owner."""


class CannotLinkToSelf(Exception):
    """Raised when an entry is linked to itself."""


class RuleNeedsSummary(Exception):
    """A rule was written without the one line the context block renders.

    Rules are the only kind injected into every session, and since the
    block renders summaries rather than bodies, a rule with no summary
    arrives as a bare title. Refusing at write time is what keeps the
    block improving; the 17 rules that predate this requirement render
    title-only and are backfilled with `remem update --summary`.

    Only raised for `origin in INJECTED_ORIGINS` - the origins `kb.resolve`
    actually renders into a context block. An EXTRACTED rule can never
    reach one (`kb.resolve` filters to INJECTED_ORIGINS), so requiring a
    summary on one is enforcement with no purpose, and it used to fail
    extraction's write of every rule the model proposed.
    """


class _Clear:
    """Sentinel meaning "set this nullable field to NULL".

    `None` already means "leave unchanged" in update(), so clearing a field
    needs a value distinct from both None and any real value.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "CLEAR"


CLEAR = _Clear()


def remember(
    store: Store,
    owner_id: UUID,
    *,
    title: str,
    body: str,
    summary: str | None = None,
    kind: Kind = Kind.NOTE,
    project: str | None = None,
    tags: list[str] | None = None,
    links: list[UUID] | None = None,
    agent: str | None = None,
    session_id: str | None = None,
    origin: Origin = Origin.AGENT,
) -> Entry:
    if (
        kind is Kind.RULE
        and origin in INJECTED_ORIGINS
        and not (summary or "").strip()
    ):
        # In the service, not the CLI: mcp_server's remember_tool accepts a
        # `kind` and would write a summary-less rule straight past a
        # frontend check. One rule enforced here is one every frontend gets.
        # Gated on INJECTED_ORIGINS, not just `kind is RULE`: an EXTRACTED
        # rule can never reach kb.resolve's output, so this is not an
        # escape hatch, it ties the requirement to its own justification.
        raise RuleNeedsSummary(title)
    entry = Entry(
        id=new_id(),
        kind=kind,
        title=title,
        body=body,
        owner_id=owner_id,
        project=project,
        summary=summary,
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
    summary: str | None = None,
    tags: list[str] | None = None,
    project: str | None | _Clear = None,
) -> Entry:
    """Change only the fields given. Pass `CLEAR` to null a field out."""
    entry = _require(store, owner_id, entry_id)
    if title is not None:
        entry.title = title
    if body is not None:
        entry.body = body
    if summary is not None:
        # No CLEAR for summary. A wrong summary is fixed by writing a
        # better one, and a rule with none renders title-only rather than
        # breaking - so emptying one has no use case worth the sentinel.
        entry.summary = summary
    if tags is not None:
        entry.tags = list(tags)
    if project is CLEAR:
        entry.project = None
    elif project is not None:
        entry.project = project
    return store.put_entry(entry)


def supersede(
    store: Store,
    owner_id: UUID,
    entry_id: UUID,
    *,
    title: str,
    body: str,
    summary: str | None = None,
) -> Entry:
    """Replace knowledge that stopped being true. The old entry is kept."""
    old = _require(store, owner_id, entry_id)
    replacement = remember(
        store,
        owner_id,
        title=title,
        body=body,
        # Carried, not defaulted to None: supersede's contract is that the
        # replacement inherits everything the caller did not restate, which
        # is what makes tag-based identity survive an edit. A summary silently
        # dropped on every supersede would empty the frontmatter description
        # of any memory file that was ever edited.
        summary=old.summary if summary is None else summary,
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
    if a_id == b_id:
        # Silently recording a self-link would make an entry look connected to
        # something when it is connected to nothing.
        raise CannotLinkToSelf(str(a_id))
    a = _require(store, owner_id, a_id)
    b = _require(store, owner_id, b_id)
    if b.id not in a.links:
        a.links.append(b.id)
        store.put_entry(a)
    if a.id not in b.links:
        b.links.append(a.id)
        store.put_entry(b)
