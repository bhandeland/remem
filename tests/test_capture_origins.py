import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import CollectionQuery, Origin, Query
from remem.services import kb, write

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def test_search_without_origins_returns_everything(store, owner):
    write.remember(store, owner.id, title="Human note", body="shared word",
                   origin=Origin.HUMAN)
    write.remember(store, owner.id, title="Captured note", body="shared word",
                   origin=Origin.CAPTURE)
    titles = {h.entry.title for h in store.search(Query(text="shared"), owner.id)}
    assert titles == {"Human note", "Captured note"}


def test_search_filters_by_origin(store, owner):
    write.remember(store, owner.id, title="Human note", body="shared word",
                   origin=Origin.HUMAN)
    write.remember(store, owner.id, title="Captured note", body="shared word",
                   origin=Origin.CAPTURE)
    hits = store.search(
        Query(text="shared", origins=[Origin.HUMAN, Origin.AGENT]), owner.id
    )
    assert [h.entry.title for h in hits] == ["Human note"]


def test_no_text_listing_also_filters_by_origin(store, owner):
    write.remember(store, owner.id, title="Human note", body="b",
                   origin=Origin.HUMAN)
    write.remember(store, owner.id, title="Captured note", body="b",
                   origin=Origin.CAPTURE)
    hits = store.search(Query(origins=[Origin.HUMAN]), owner.id)
    assert [h.entry.title for h in hits] == ["Human note"]


def test_fuzzy_search_also_filters_by_origin(store, owner):
    """A filter honoured by only one search path would appear to work until a
    query happened to miss exactly."""
    write.remember(store, owner.id, title="Postgres connection pooling",
                   body="b", origin=Origin.CAPTURE)
    hits = store.fuzzy_search(
        Query(text="postgres conection pooling", origins=[Origin.HUMAN]),
        owner.id,
        0.3,
    )
    assert hits == []


def test_resolve_excludes_captured_entries_from_the_query_branch(store, owner):
    kb.create(store, owner.id, slug="s", title="T",
              query=CollectionQuery(tags=["ops"]))
    write.remember(store, owner.id, title="Human note", body="b",
                   tags=["ops"], origin=Origin.HUMAN)
    write.remember(store, owner.id, title="Captured note", body="b",
                   tags=["ops"], origin=Origin.CAPTURE)
    assert [e.title for e in kb.resolve(store, owner.id, "s")] == ["Human note"]


def test_resolve_includes_a_captured_entry_that_was_pinned(store, owner):
    """Pinning is the deliberate way to promote a captured entry."""
    kb.create(store, owner.id, slug="s", title="T")
    captured = write.remember(store, owner.id, title="Captured note", body="b",
                              origin=Origin.CAPTURE)
    kb.pin(store, owner.id, "s", captured.id)
    assert [e.title for e in kb.resolve(store, owner.id, "s")] == ["Captured note"]
