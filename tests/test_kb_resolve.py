import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import CollectionQuery, Kind
from remem.services import kb
from remem.services.write import remember, supersede

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def test_create_then_resolve_an_empty_collection(store, owner):
    kb.create(store, owner.id, slug="s", title="T")
    assert kb.resolve(store, owner.id, "s") == []


def test_resolve_returns_pinned_entries(store, owner):
    c = kb.create(store, owner.id, slug="s", title="T")
    e = remember(store, owner.id, title="Pinned", body="b")
    store.pin(c.id, e.id, position=0, owner_id=owner.id)
    assert [x.title for x in kb.resolve(store, owner.id, "s")] == ["Pinned"]


def test_resolve_includes_query_matches(store, owner):
    kb.create(store, owner.id, slug="s", title="T",
              query=CollectionQuery(tags=["style"]))
    remember(store, owner.id, title="Styled", body="b", tags=["style"])
    remember(store, owner.id, title="Other", body="b", tags=["other"])
    assert [x.title for x in kb.resolve(store, owner.id, "s")] == ["Styled"]


def test_resolve_dedupes_when_an_entry_is_both_pinned_and_matched(store, owner):
    c = kb.create(store, owner.id, slug="s", title="T",
                  query=CollectionQuery(tags=["style"]))
    e = remember(store, owner.id, title="Both", body="b", tags=["style"])
    store.pin(c.id, e.id, position=0, owner_id=owner.id)
    assert [x.title for x in kb.resolve(store, owner.id, "s")] == ["Both"]


def test_pinned_entries_come_before_query_matches(store, owner):
    c = kb.create(store, owner.id, slug="s", title="T",
                  query=CollectionQuery(tags=["style"]))
    matched = remember(store, owner.id, title="Matched", body="b", tags=["style"])
    pinned = remember(store, owner.id, title="Pinned", body="b")
    store.pin(c.id, pinned.id, position=0, owner_id=owner.id)
    titles = [x.title for x in kb.resolve(store, owner.id, "s")]
    assert titles.index("Pinned") < titles.index("Matched")


def test_resolve_excludes_superseded_entries(store, owner):
    kb.create(store, owner.id, slug="s", title="T",
              query=CollectionQuery(tags=["deploys"]))
    old = remember(store, owner.id, title="Old", body="b", tags=["deploys"])
    supersede(store, owner.id, old.id, title="New", body="b")
    titles = [x.title for x in kb.resolve(store, owner.id, "s")]
    assert "Old" not in titles


def test_resolve_filters_by_kind_in_the_query(store, owner):
    kb.create(store, owner.id, slug="s", title="T",
              query=CollectionQuery(kinds=[Kind.RULE]))
    remember(store, owner.id, title="A rule", body="b", kind=Kind.RULE)
    remember(store, owner.id, title="A memory", body="b", kind=Kind.MEMORY)
    assert [x.title for x in kb.resolve(store, owner.id, "s")] == ["A rule"]


def test_resolve_raises_for_an_unknown_slug(store, owner):
    with pytest.raises(kb.CollectionNotFound):
        kb.resolve(store, owner.id, "nope")


def test_an_empty_query_matches_nothing_rather_than_everything(store, owner):
    kb.create(store, owner.id, slug="s", title="T")
    remember(store, owner.id, title="Loose", body="b")
    assert kb.resolve(store, owner.id, "s") == []


def test_a_pinned_entry_that_is_later_superseded_does_not_resurface(store, owner):
    c = kb.create(store, owner.id, slug="s", title="T")
    e = remember(store, owner.id, title="Old", body="b")
    store.pin(c.id, e.id, position=0, owner_id=owner.id)
    supersede(store, owner.id, e.id, title="New", body="b")
    titles = [x.title for x in kb.resolve(store, owner.id, "s")]
    assert "Old" not in titles
