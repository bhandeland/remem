"""Embedding backlog policy: what needs work, in what batches, and what to do
when a batch fails."""

from __future__ import annotations

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
            store.put_vector(entry.id, embedder.name, len(vector), vector,
                             owner_id)
            embedded += 1

        if remaining is not None:
            remaining -= len(batch)

    return EmbedResult(embedded=embedded, failed=failed, model=embedder.name)
