"""Visibility rules for the two ingest origins.

Mirrors test_extract_origins.py and test_handoff_visibility.py. The
DEFAULT_ORIGINS comment in services/search.py warns that a new origin not
added to that list vanishes from search silently; this is the test that
makes the warning bite.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import CollectionQuery, Kind, Origin, Principal, Query
from saddlebag.services import kb, search
from saddlebag.services.write import remember

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


def test_ingested_is_in_the_default_origins_and_archived_is_not() -> None:
    assert Origin.INGESTED in search.DEFAULT_ORIGINS
    assert Origin.ARCHIVED not in search.DEFAULT_ORIGINS


def test_default_search_finds_ingested_and_hides_archived(
    store: PostgresStore, owner: Principal
) -> None:
    remember(
        store,
        owner.id,
        title="Sweep design",
        body="the sweep supersedes orphans",
        kind=Kind.DOC,
        project="saddlebag",
        origin=Origin.INGESTED,
    )
    remember(
        store,
        owner.id,
        title="Task 9 sweep",
        body="the sweep supersedes orphans",
        kind=Kind.DOC,
        project="saddlebag",
        origin=Origin.ARCHIVED,
    )

    titles = [h.entry.title for h in search.find(store, owner.id, Query(text="sweep"))]
    assert titles == ["Sweep design"]


def test_include_archived_surfaces_both(store: PostgresStore, owner: Principal) -> None:
    remember(
        store,
        owner.id,
        title="Sweep design",
        body="the sweep supersedes orphans",
        kind=Kind.DOC,
        project="saddlebag",
        origin=Origin.INGESTED,
    )
    remember(
        store,
        owner.id,
        title="Task 9 sweep",
        body="the sweep supersedes orphans",
        kind=Kind.DOC,
        project="saddlebag",
        origin=Origin.ARCHIVED,
    )

    hits = search.find(store, owner.id, Query(text="sweep"), include_archived=True)
    assert {h.entry.title for h in hits} == {"Sweep design", "Task 9 sweep"}


def test_neither_origin_reaches_a_context_block(
    store: PostgresStore, owner: Principal
) -> None:
    remember(
        store,
        owner.id,
        title="Ingested rule",
        body="never in context",
        summary="Ingested docs stay out of every session's context",
        kind=Kind.RULE,
        project="saddlebag",
        origin=Origin.INGESTED,
    )
    remember(
        store,
        owner.id,
        title="Archived rule",
        body="never in context",
        summary="Archived plans stay out of every session's context",
        kind=Kind.RULE,
        project="saddlebag",
        origin=Origin.ARCHIVED,
    )
    kb.create(
        store,
        owner.id,
        slug="saddlebag",
        title="saddlebag",
        query=CollectionQuery(project="saddlebag"),
    )

    # kb.resolve takes the collection's SLUG, not the object.
    entries = kb.resolve(store, owner.id, "saddlebag")
    assert entries == []
