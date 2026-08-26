"""Query hygiene lives here so every frontend gets it for free."""

from __future__ import annotations

from uuid import UUID

from remem.domain import Hit, Query
from remem.store import Store

MAX_LIMIT = 200


def find(store: Store, owner_id: UUID, query: Query) -> list[Hit]:
    if query.limit <= 0:
        return []
    if query.limit > MAX_LIMIT:
        query = Query(
            text=query.text,
            kinds=query.kinds,
            project=query.project,
            tags=query.tags,
            since=query.since,
            include_superseded=query.include_superseded,
            limit=MAX_LIMIT,
        )
    return store.search(query, owner_id)
