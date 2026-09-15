"""`report()` over a real store: suppression and coverage end to end."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import Entry, Kind, Query, new_id
from saddlebag.services.dedupe import report

pytestmark = pytest.mark.db

MODEL = "test-2d"


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


def _entry(store: PostgresStore, owner_id: UUID, title: str, body: str) -> Entry:
    return store.put_entry(
        Entry(id=new_id(), kind=Kind.NOTE, title=title, body=body, owner_id=owner_id)
    )


def test_an_exact_group_is_not_also_reported_as_a_near_pair(
    store: PostgresStore,
) -> None:
    owner = store.ensure_principal("report-suppress")
    a = _entry(store, owner.id, "a", "same body")
    b = _entry(store, owner.id, "b", "same body")
    store.put_vector(a.id, MODEL, 2, [1.0, 0.0], owner.id)
    store.put_vector(b.id, MODEL, 2, [1.0, 0.0], owner.id)

    r = report(store, owner.id, Query(limit=50), MODEL, threshold=0.9)

    assert len(r.exact) == 1
    assert r.near == []
    # The store still counted it - the suppression is the service's.
    assert r.near_total == 1


def test_coverage_is_reported_when_nothing_is_embedded(store: PostgresStore) -> None:
    owner = store.ensure_principal("report-coverage")
    _entry(store, owner.id, "a", "one")
    _entry(store, owner.id, "b", "two")

    r = report(store, owner.id, Query(limit=50), MODEL)

    assert (r.embedded, r.total) == (0, 2)
    assert r.near == []
