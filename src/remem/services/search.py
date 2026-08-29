"""Query hygiene lives here so every frontend gets it for free."""

from __future__ import annotations

from dataclasses import replace
from typing import Final
from uuid import UUID

from remem.config import DEFAULT_FUZZY_THRESHOLD, DEFAULT_SEMANTIC_THRESHOLD
from remem.domain import Hit, Origin, Query
from remem.embed import (
    DEFAULT_EMBED_MODEL,
    Embedder,
    EmbedderUnavailable,
    load_embedder,
)
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

#: The default value of `find(embedder=...)`, and not the same thing as None.
#:
#: None is a caller saying "there is no embedder, skip the semantic tier".
#: This sentinel is a caller saying nothing at all, which is every frontend,
#: and means "build the shared one if and when the semantic tier is reached".
#: Collapsing the two would make an explicit `embedder=None` silently grow an
#: embedder - including in the tests that pass it to assert the two-tier
#: degradation, where it would cost a 130MB model download.
_UNSPECIFIED: Final = object()

#: Embedders memoised by model name.
#:
#: Constructing a LocalEmbedder imports fastembed, builds an ONNX session and
#: runs an inference call to probe the dimension - a fifth of a second warm,
#: and a ~130MB download cold. The MCP server is a long-lived process that
#: would otherwise pay that on every single recall call.
#:
#: A None value is cached too: an embedder that is unavailable stays
#: unavailable for the life of the process, and re-attempting the import on
#: every search would repay the failure without ever changing the answer.
#:
#: Tests never populate this - they pass an embedder (or None) explicitly, and
#: the sentinel above is what keeps those two paths from touching this cache.
_EMBEDDERS: dict[str, Embedder | None] = {}


def shared_embedder(model_name: str = DEFAULT_EMBED_MODEL) -> Embedder | None:
    """The embedder search uses, built at most once per model name.

    This is a policy decision and it lives here rather than in each frontend
    on purpose: "an unavailable embedder is a None, not an error" is a rule
    about how search degrades, and a rule enforced in the service is one that
    every frontend - the CLI, the MCP server, and whatever comes next - gets
    for free instead of re-deriving with its own try/except.

    Note what does NOT live here: `remem embed` calls `load_embedder`
    directly and fails loudly, because there an unavailable embedder is the
    command failing at its entire job rather than a tier quietly missing.
    """
    if model_name in _EMBEDDERS:
        return _EMBEDDERS[model_name]
    try:
        embedder: Embedder | None = load_embedder(model_name)
    except EmbedderUnavailable:
        embedder = None
    _EMBEDDERS[model_name] = embedder
    return embedder


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
    embedder: Embedder | None = _UNSPECIFIED,  # type: ignore[assignment]
    semantic_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
    embed_model: str = DEFAULT_EMBED_MODEL,
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

    Left unspecified - which is what every frontend does - the embedder is
    the shared one for `embed_model`, and it is built inside the semantic
    tier rather than here. That ordering is the point: an exact match returns
    above, so a search the exact tier can answer never imports fastembed, and
    never triggers the model download that importing it can start. Pass an
    explicit embedder (or an explicit None) to override, as the tests do.

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

    hits = _semantic(store, owner_id, query, text, embedder,
                     semantic_threshold, embed_model)
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
    embed_model: str,
) -> list[Hit]:
    """The middle tier, and everything that can go wrong with it.

    Kept separate so the degradation reads as one idea rather than three
    try/excepts inside the tier chain. Every failure here means the same
    thing to the caller: no semantic results, carry on to trigram.

    This is also the only place an embedder gets built for a search, and it
    is reached only after the exact tier came back empty - so the cost of
    constructing one is paid by the searches that can actually use it.
    """
    if embedder is _UNSPECIFIED:
        embedder = shared_embedder(embed_model)
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
