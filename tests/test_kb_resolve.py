from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import CollectionQuery, Kind, Principal, new_id
from saddlebag.services import kb
from saddlebag.services.write import remember, supersede

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


def test_create_then_resolve_an_empty_collection(
    store: PostgresStore, owner: Principal
) -> None:
    kb.create(store, owner.id, slug="s", title="T")
    assert kb.resolve(store, owner.id, "s") == []


def test_resolve_returns_pinned_entries(store: PostgresStore, owner: Principal) -> None:
    c = kb.create(store, owner.id, slug="s", title="T")
    e = remember(store, owner.id, title="Pinned", body="b")
    store.pin(c.id, e.id, position=0, owner_id=owner.id)
    assert [x.title for x in kb.resolve(store, owner.id, "s")] == ["Pinned"]


def test_resolve_includes_query_matches(store: PostgresStore, owner: Principal) -> None:
    kb.create(
        store, owner.id, slug="s", title="T", query=CollectionQuery(tags=["style"])
    )
    remember(store, owner.id, title="Styled", body="b", tags=["style"])
    remember(store, owner.id, title="Other", body="b", tags=["other"])
    assert [x.title for x in kb.resolve(store, owner.id, "s")] == ["Styled"]


def test_resolve_dedupes_when_an_entry_is_both_pinned_and_matched(
    store: PostgresStore, owner: Principal
) -> None:
    c = kb.create(
        store, owner.id, slug="s", title="T", query=CollectionQuery(tags=["style"])
    )
    e = remember(store, owner.id, title="Both", body="b", tags=["style"])
    store.pin(c.id, e.id, position=0, owner_id=owner.id)
    assert [x.title for x in kb.resolve(store, owner.id, "s")] == ["Both"]


def test_pinned_entries_come_before_query_matches(
    store: PostgresStore, owner: Principal
) -> None:
    c = kb.create(
        store, owner.id, slug="s", title="T", query=CollectionQuery(tags=["style"])
    )
    remember(store, owner.id, title="Matched", body="b", tags=["style"])
    pinned = remember(store, owner.id, title="Pinned", body="b")
    store.pin(c.id, pinned.id, position=0, owner_id=owner.id)
    titles = [x.title for x in kb.resolve(store, owner.id, "s")]
    assert titles.index("Pinned") < titles.index("Matched")


def test_resolve_excludes_superseded_entries(
    store: PostgresStore, owner: Principal
) -> None:
    kb.create(
        store, owner.id, slug="s", title="T", query=CollectionQuery(tags=["deploys"])
    )
    old = remember(store, owner.id, title="Old", body="b", tags=["deploys"])
    supersede(store, owner.id, old.id, title="New", body="b")
    titles = [x.title for x in kb.resolve(store, owner.id, "s")]
    assert "Old" not in titles


def test_resolve_filters_by_kind_in_the_query(
    store: PostgresStore, owner: Principal
) -> None:
    kb.create(
        store, owner.id, slug="s", title="T", query=CollectionQuery(kinds=[Kind.RULE])
    )
    remember(
        store,
        owner.id,
        title="A rule",
        body="b",
        summary="A rule that filters by kind",
        kind=Kind.RULE,
    )
    remember(store, owner.id, title="A memory", body="b", kind=Kind.NOTE)
    assert [x.title for x in kb.resolve(store, owner.id, "s")] == ["A rule"]


def test_resolve_raises_for_an_unknown_slug(
    store: PostgresStore, owner: Principal
) -> None:
    with pytest.raises(kb.CollectionNotFound):
        kb.resolve(store, owner.id, "nope")


def test_an_empty_query_matches_nothing_rather_than_everything(
    store: PostgresStore, owner: Principal
) -> None:
    kb.create(store, owner.id, slug="s", title="T")
    remember(store, owner.id, title="Loose", body="b")
    assert kb.resolve(store, owner.id, "s") == []


def test_a_pinned_entry_that_is_later_superseded_does_not_resurface(
    store: PostgresStore, owner: Principal
) -> None:
    c = kb.create(store, owner.id, slug="s", title="T")
    e = remember(store, owner.id, title="Old", body="b")
    store.pin(c.id, e.id, position=0, owner_id=owner.id)
    supersede(store, owner.id, e.id, title="New", body="b")
    titles = [x.title for x in kb.resolve(store, owner.id, "s")]
    assert "Old" not in titles


def test_pin_adds_an_entry_to_the_collection(
    store: PostgresStore, owner: Principal
) -> None:
    kb.create(store, owner.id, slug="s", title="T")
    e = remember(store, owner.id, title="Pinned", body="b")
    kb.pin(store, owner.id, "s", e.id)
    assert [x.title for x in kb.resolve(store, owner.id, "s")] == ["Pinned"]


def test_pin_rejects_an_unknown_collection(
    store: PostgresStore, owner: Principal
) -> None:
    e = remember(store, owner.id, title="E", body="b")
    with pytest.raises(kb.CollectionNotFound):
        kb.pin(store, owner.id, "nope", e.id)


def test_pin_rejects_an_unknown_entry(store: PostgresStore, owner: Principal) -> None:
    kb.create(store, owner.id, slug="s", title="T")
    with pytest.raises(kb.EntryNotFound):
        kb.pin(store, owner.id, "s", new_id())


def test_pin_rejects_another_owners_entry(
    store: PostgresStore, owner: Principal
) -> None:
    other = store.ensure_principal("mallory")
    kb.create(store, owner.id, slug="s", title="T")
    theirs = remember(store, other.id, title="Theirs", body="b")
    with pytest.raises(kb.EntryNotFound):
        kb.pin(store, owner.id, "s", theirs.id)
    assert kb.resolve(store, owner.id, "s") == []
