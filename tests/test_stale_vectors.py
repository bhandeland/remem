"""An edited entry must not stay findable by its old wording.

This one cannot be caught by a marker. A semantic hit on a stale vector is a
legitimate hit by the tier's own rules - the vector really is close to the
query - so nothing downstream can warn about it. The only place to fix it is
at the write, by deleting the derived row the moment its source changes.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import Principal, Query
from saddlebag.services import write

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


def test_editing_the_body_drops_the_vector(
    store: PostgresStore, owner: Principal
) -> None:
    entry = write.remember(
        store, owner.id, title="pgvector indexing", body="original text"
    )
    store.put_vector(entry.id, "m", 3, [1.0, 0.0, 0.0], owner.id)

    write.update(store, owner.id, entry.id, body="entirely different text")

    missing = store.entries_missing_vectors(owner.id, "m", limit=10)
    assert [e.id for e in missing] == [entry.id]


def test_a_write_that_changes_no_text_keeps_the_vector(
    store: PostgresStore, owner: Principal
) -> None:
    """Re-embedding on every touch would make `bag embed` never finish.

    Linking two entries calls put_entry, and so does superseding. Neither
    changes what the entry says, so neither invalidates what it means.
    """
    entry = write.remember(store, owner.id, title="t", body="b")
    store.put_vector(entry.id, "m", 3, [1.0, 0.0, 0.0], owner.id)

    write.update(store, owner.id, entry.id, tags=["new-tag"])

    assert store.entries_missing_vectors(owner.id, "m", limit=10) == []


def test_the_semantic_tier_cannot_return_the_stale_wording(
    store: PostgresStore, owner: Principal
) -> None:
    """The end-to-end version of the first test, stated as search behaviour."""
    entry = write.remember(store, owner.id, title="t", body="original")
    store.put_vector(entry.id, "m", 3, [1.0, 0.0, 0.0], owner.id)
    write.update(store, owner.id, entry.id, body="different")

    hits = store.semantic_search(
        Query(text="original", limit=10),
        owner.id,
        [1.0, 0.0, 0.0],
        "m",
        0.5,
    )
    assert hits == []
