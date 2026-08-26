import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Collection, CollectionQuery, Kind, new_id
from remem.services.write import remember

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def test_put_and_get_collection_roundtrip(store, owner):
    c = Collection(
        id=new_id(), slug="remem-core", title="remem core", owner_id=owner.id,
        description="the important bits", project="remem",
        query=CollectionQuery(tags=["style"], kinds=[Kind.RULE], project="remem"),
    )
    store.put_collection(c)
    got = store.get_collection("remem-core", owner.id)
    assert got.title == "remem core"
    assert got.description == "the important bits"
    assert got.query.tags == ["style"]
    assert got.query.kinds == [Kind.RULE]
    assert got.query.project == "remem"


def test_get_collection_is_owner_scoped(store, owner):
    other = store.ensure_principal("someone-else")
    store.put_collection(
        Collection(id=new_id(), slug="s", title="T", owner_id=owner.id)
    )
    assert store.get_collection("s", other.id) is None


def test_list_collections_returns_only_mine(store, owner):
    other = store.ensure_principal("someone-else")
    store.put_collection(Collection(id=new_id(), slug="a", title="A", owner_id=owner.id))
    store.put_collection(Collection(id=new_id(), slug="b", title="B", owner_id=other.id))
    assert [c.slug for c in store.list_collections(owner.id)] == ["a"]


def test_pin_and_read_back_in_position_order(store, owner):
    c = Collection(id=new_id(), slug="s", title="T", owner_id=owner.id)
    store.put_collection(c)
    first = remember(store, owner.id, title="First", body="b")
    second = remember(store, owner.id, title="Second", body="b")
    store.pin(c.id, second.id, position=0)
    store.pin(c.id, first.id, position=1)
    assert [e.title for e in store.pinned_entries(c.id, owner.id)] == ["Second", "First"]


def test_pinning_twice_updates_position_instead_of_erroring(store, owner):
    c = Collection(id=new_id(), slug="s", title="T", owner_id=owner.id)
    store.put_collection(c)
    e = remember(store, owner.id, title="E", body="b")
    store.pin(c.id, e.id, position=0)
    store.pin(c.id, e.id, position=5)
    assert len(store.pinned_entries(c.id, owner.id)) == 1


def test_slug_uniqueness_is_scoped_per_owner_not_global(store, owner):
    other = store.ensure_principal("someone-else")
    store.put_collection(
        Collection(id=new_id(), slug="core", title="Alice's KB", owner_id=owner.id)
    )
    store.put_collection(
        Collection(id=new_id(), slug="core", title="Bob's KB", owner_id=other.id)
    )

    mine = store.get_collection("core", owner.id)
    theirs = store.get_collection("core", other.id)

    assert mine is not None
    assert theirs is not None
    assert mine.title == "Alice's KB"
    assert theirs.title == "Bob's KB"
    assert mine.id != theirs.id
    assert [c.slug for c in store.list_collections(owner.id)] == ["core"]
    assert [c.slug for c in store.list_collections(other.id)] == ["core"]
