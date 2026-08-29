"""Query hygiene lives here so every frontend gets it for free."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from remem.config import DEFAULT_FUZZY_THRESHOLD, DEFAULT_SEMANTIC_THRESHOLD
from remem.domain import Hit, Origin, Query
from remem.embed import Embedder
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
    return replace(query, limit=MAX_LIMIT)


def find(
    store: Store,
    owner_id: UUID,
    query: Query,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    include_handoffs: bool = False,
    embedder: Embedder | None = None,
    semantic_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
) -> list[Hit]:
    """Three tiers, each running only when the one above returned nothing.

        exact full-text  ->  semantic  ->  trigram

    Fallback rather than blending, for the reason the two-tier version
    already documented: mixing approximate hits into a result set that
    contains exact ones trades precision for a problem that does not exist
    there. With three tiers that matters more, not less - a result set
    holding all three kinds would need the caller to reason about which
    ranking each number came from, and they are not comparable.

    Semantic sits above trigram because meaning beats spelling. A query that
    matches nothing lexically is far more often a different wording than a
    typo, and trigram remains what it always was: the typo net, tried last.

    `embedder` is optional and its absence is not an error. The local
    embedder is an optional dependency; without it search degrades to the two
    tiers it has always had. Same for an embedder that fails at query time -
    the user asked a question, and two tiers can still answer it.

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
    text = (query.text or "").strip()
    if hits or not text:
        # No text means a listing query - filters only. There is nothing for
        # the fallbacks to be approximately like.
        return hits

    hits = _semantic(store, owner_id, query, text, embedder, semantic_threshold)
    if hits:
        return hits

    return store.fuzzy_search(query, owner_id, fuzzy_threshold)


def _semantic(
    store: Store,
    owner_id: UUID,
    query: Query,
    text: str,
    embedder: Embedder | None,
    threshold: float,
) -> list[Hit]:
    """The middle tier, and everything that can go wrong with it.

    Kept separate so the degradation reads as one idea rather than three
    try/excepts inside the tier chain. Every failure here means the same
    thing to the caller: no semantic results, carry on to trigram.
    """
    if embedder is None:
        return []
    try:
        vectors = embedder.embed([text])
    except Exception:
        # A missing model file, a corrupt download, an out-of-memory ONNX
        # session. All of them cost this tier and none of them should cost
        # the search. Deliberately broad: the failure modes of a model
        # runtime are not enumerable, and the response is the same for all.
        return []
    if not vectors:
        return []
    return store.semantic_search(query, owner_id, vectors[0], embedder.name,
                                 threshold)
