import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Entry, Kind, Origin, new_id

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def test_ensure_principal_creates_then_returns_the_same_one(store):
    a = store.ensure_principal("brandon")
    b = store.ensure_principal("brandon")
    assert a.id == b.id
    assert a.handle == "brandon"


def test_get_principal_returns_none_when_absent(store):
    assert store.get_principal("nobody") is None


def test_put_and_get_entry_roundtrip(store, owner):
    e = Entry(
        id=new_id(),
        kind=Kind.RULE,
        title="Use spaced hyphens",
        body="Never em dashes.",
        owner_id=owner.id,
        project="remem",
        tags=["style", "writing"],
        agent="claude-code",
        session_id="sess-1",
        origin=Origin.HUMAN,
    )
    store.put_entry(e)
    got = store.get_entry(e.id, owner.id)
    assert got is not None
    assert got.title == "Use spaced hyphens"
    assert got.kind is Kind.RULE
    assert got.tags == ["style", "writing"]
    assert got.project == "remem"
    assert got.origin is Origin.HUMAN
    assert got.session_id == "sess-1"
    assert got.created_at is not None


def test_get_entry_is_scoped_to_the_owner(store, owner):
    other = store.ensure_principal("someone-else")
    e = Entry(id=new_id(), kind=Kind.MEMORY, title="t", body="b", owner_id=owner.id)
    store.put_entry(e)
    assert store.get_entry(e.id, other.id) is None


def test_put_entry_updates_an_existing_row(store, owner):
    e = Entry(id=new_id(), kind=Kind.DOC, title="v1", body="b", owner_id=owner.id)
    store.put_entry(e)
    e.title = "v2"
    store.put_entry(e)
    got = store.get_entry(e.id, owner.id)
    assert got.title == "v2"
    assert got.updated_at >= got.created_at


def test_links_roundtrip_as_uuids(store, owner):
    target = Entry(id=new_id(), kind=Kind.DOC, title="target", body="b", owner_id=owner.id)
    store.put_entry(target)
    e = Entry(
        id=new_id(), kind=Kind.MEMORY, title="src", body="b",
        owner_id=owner.id, links=[target.id],
    )
    store.put_entry(e)
    assert store.get_entry(e.id, owner.id).links == [target.id]


def test_set_superseded_marks_the_old_entry(store, owner):
    old = Entry(id=new_id(), kind=Kind.MEMORY, title="old", body="b", owner_id=owner.id)
    new = Entry(id=new_id(), kind=Kind.MEMORY, title="new", body="b", owner_id=owner.id)
    store.put_entry(old)
    store.put_entry(new)
    assert store.set_superseded(old.id, new.id, owner.id) is True
    assert store.get_entry(old.id, owner.id).superseded_by == new.id


def test_set_superseded_refuses_across_owners(store, owner):
    other = store.ensure_principal("someone-else")
    old = Entry(id=new_id(), kind=Kind.MEMORY, title="old", body="b", owner_id=owner.id)
    new = Entry(id=new_id(), kind=Kind.MEMORY, title="new", body="b", owner_id=owner.id)
    store.put_entry(old)
    store.put_entry(new)
    assert store.set_superseded(old.id, new.id, other.id) is False


def test_set_superseded_refuses_when_new_entry_belongs_to_another_owner(store, owner):
    other = store.ensure_principal("someone-else")
    old = Entry(id=new_id(), kind=Kind.MEMORY, title="old", body="b", owner_id=owner.id)
    foreign_new = Entry(id=new_id(), kind=Kind.MEMORY, title="new", body="b", owner_id=other.id)
    store.put_entry(old)
    store.put_entry(foreign_new)
    assert store.set_superseded(old.id, foreign_new.id, owner.id) is False
    assert store.get_entry(old.id, owner.id).superseded_by is None
