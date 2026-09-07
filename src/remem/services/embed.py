"""Embedding backlog policy: what needs work, in what batches, and what to do
when a batch fails."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from remem.domain import Entry
from remem.embed import Embedder
from remem.store import Store

DEFAULT_BATCH_SIZE = 32


@dataclass(frozen=True, slots=True)
class EmbedResult:
    embedded: int
    failed: int
    model: str


def embed_text(entry: Entry) -> str:
    """What actually gets embedded.

    Title and body together. In a short entry the title carries most of the
    signal, and a body-only embedding fails to match a query that is almost
    word-for-word the title - the single most likely query there is. Tags are
    left out: they are already weighted in the full-text tier, and a bag of
    slugs dilutes a sentence embedding rather than sharpening it.
    """
    return f"{entry.title}\n\n{entry.body}"


def backfill(
    store: Store,
    owner_id: UUID,
    embedder: Embedder,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_entries: int | None = None,
) -> EmbedResult:
    """Embed every entry lacking a vector for the embedder's model.

    Idempotent, and safe to re-run: the work is defined by what is missing,
    not by a cursor or a queue, so an interrupted run simply leaves a shorter
    backlog for the next one.

    A batch that raises is counted and skipped rather than propagated. The
    caller is a cron command with a backlog possibly in the hundreds, and
    abandoning all of it because one batch upset the model is the wrong
    trade. The count is what makes the failure visible.
    """
    embedded = 0
    failed = 0
    remaining = max_entries

    while True:
        want = batch_size if remaining is None else min(batch_size, remaining)
        if want <= 0:
            break
        batch = store.entries_missing_vectors(owner_id, embedder.name, want)
        if not batch:
            break

        try:
            vectors = embedder.embed([embed_text(e) for e in batch])
        except Exception:
            failed += len(batch)
            # Stop rather than continue: entries_missing_vectors would hand
            # back this very batch again on the next pass, and a model that
            # fails once fails the same way in a loop. The next scheduled run
            # is the retry.
            break

        for entry, vector in zip(batch, vectors, strict=True):
            store.put_vector(entry.id, embedder.name, len(vector), vector, owner_id)
            embedded += 1

        if remaining is not None:
            remaining -= len(batch)

    return EmbedResult(embedded=embedded, failed=failed, model=embedder.name)


def backfill_if_pending(
    store: Store,
    owner_id: UUID,
    model: str,
    load: Callable[[], Embedder],
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_entries: int | None = None,
) -> EmbedResult | None:
    """Backfill, but only build the embedder if there is anything to embed.

    `backfill` takes an Embedder already constructed, which is right for
    `remem embed`: a user asked for it, so paying for it is the point.
    Automatic callers are the opposite - they run on every session start,
    and constructing a LocalEmbedder imports fastembed, builds an ONNX
    session and can download ~130MB, all for a backlog that is empty almost
    every time. The model NAME is enough to ask whether work is waiting,
    which is what makes the check possible before the cost.

    This is the same policy `services.search.shared_embedder` applies inside
    the semantic tier, for the same reason, applied in a second place.

    Returns None when there was nothing to do - distinct from an
    EmbedResult with `embedded=0`, which would mean the model was built and
    then found nothing. `load` raising is propagated: whether an unavailable
    embedder is fatal is the caller's policy (loud for `remem embed`,
    swallowed for the spawned refresh), and collapsing it into None here
    would make those two indistinguishable.
    """
    if not store.entries_missing_vectors(owner_id, model, 1):
        return None
    return backfill(
        store, owner_id, load(), batch_size=batch_size, max_entries=max_entries
    )
