"""Query hygiene lives here so every frontend gets it for free."""

from __future__ import annotations

from uuid import UUID

from remem.config import DEFAULT_FUZZY_THRESHOLD
from remem.domain import Hit, Query
from remem.store import Store

MAX_LIMIT = 200


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
        limit=MAX_LIMIT,
    )


def find(
    store: Store,
    owner_id: UUID,
    query: Query,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> list[Hit]:
    """Exact search, falling back to typo-tolerant matching only when it finds
    nothing.

    Fallback rather than blending: full-text search matches lexemes, so a
    misspelled query matches nothing at all and the caller concludes nothing is
    stored. Trigram similarity fixes exactly that case. Mixing fuzzy hits into
    a result set that already contains exact ones would trade precision for a
    problem that does not exist there, so the fallback runs only on an empty
    result. Every hit it returns is marked `fuzzy=True`.
    """
    if query.limit <= 0:
        return []
    query = _clamped(query)

    hits = store.search(query, owner_id)
    if hits or not (query.text or "").strip():
        return hits

    return store.fuzzy_search(query, owner_id, fuzzy_threshold)
