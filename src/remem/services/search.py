"""Query hygiene lives here so every frontend gets it for free."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from remem.config import DEFAULT_FUZZY_THRESHOLD
from remem.domain import Hit, Origin, Query
from remem.store import Store

MAX_LIMIT = 200

#: What a search returns when the caller did not ask for specific origins.
#: Handoffs are excluded: a project hands off dozens of times and every one of
#: them would otherwise sit on top of the results.
#:
#: This list must gain any future origin, or that origin silently vanishes
#: from search. The alternative - an `exclude_origins` field on Query - avoids
#: that at the cost of a second overlapping filter in the store's SQL for one
#: caller. Chosen deliberately; if a fourth origin appears, look here.
DEFAULT_ORIGINS = [Origin.HUMAN, Origin.AGENT, Origin.CAPTURE]


def _clamped(query: Query) -> Query:
    if query.limit <= MAX_LIMIT:
        return query
    return Query(
        text=query.text,
        kinds=query.kinds,
        project=query.project,
        tags=query.tags,
        since=query.since,
        include_superseded=query.include_superseded,
        origins=query.origins,
        limit=MAX_LIMIT,
    )


def find(
    store: Store,
    owner_id: UUID,
    query: Query,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    include_handoffs: bool = False,
) -> list[Hit]:
    """Exact search, falling back to typo-tolerant matching only when it finds
    nothing.

    Fallback rather than blending: full-text search matches lexemes, so a
    misspelled query matches nothing at all and the caller concludes nothing is
    stored. Trigram similarity fixes exactly that case. Mixing fuzzy hits into
    a result set that already contains exact ones would trade precision for a
    problem that does not exist there, so the fallback runs only on an empty
    result. Every hit it returns is marked `fuzzy=True`.

    Handoffs are excluded from the default origins unless `include_handoffs`
    is set or the caller already named specific origins.
    """
    if query.limit <= 0:
        return []
    if not include_handoffs and not query.origins:
        # An explicit origins list is the caller saying exactly what they
        # want, and is never overridden.
        query = replace(query, origins=list(DEFAULT_ORIGINS))
    query = _clamped(query)

    hits = store.search(query, owner_id)
    if hits or not (query.text or "").strip():
        return hits

    return store.fuzzy_search(query, owner_id, fuzzy_threshold)
