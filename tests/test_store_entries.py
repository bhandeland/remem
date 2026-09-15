from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import Entry, Kind, Origin, Principal, new_id
from tests.conftest import found

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


def test_ensure_principal_creates_then_returns_the_same_one(
    store: PostgresStore,
) -> None:
    a = store.ensure_principal("brandon")
    b = store.ensure_principal("brandon")
    assert a.id == b.id
    assert a.handle == "brandon"


def test_get_principal_returns_none_when_absent(store: PostgresStore) -> None:
    assert store.get_principal("nobody") is None


def test_put_and_get_entry_roundtrip(store: PostgresStore, owner: Principal) -> None:
    e = Entry(
        id=new_id(),
        kind=Kind.RULE,
        title="Use spaced hyphens",
        body="Never em dashes.",
        owner_id=owner.id,
        project="saddlebag",
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
    assert got.project == "saddlebag"
    assert got.origin is Origin.HUMAN
    assert got.session_id == "sess-1"
    assert got.created_at is not None


def test_get_entry_is_scoped_to_the_owner(
    store: PostgresStore, owner: Principal
) -> None:
    other = store.ensure_principal("someone-else")
    e = Entry(id=new_id(), kind=Kind.NOTE, title="t", body="b", owner_id=owner.id)
    store.put_entry(e)
    assert store.get_entry(e.id, other.id) is None


def test_put_entry_updates_an_existing_row(
    store: PostgresStore, owner: Principal
) -> None:
    e = Entry(id=new_id(), kind=Kind.DOC, title="v1", body="b", owner_id=owner.id)
    store.put_entry(e)
    e.title = "v2"
    store.put_entry(e)
    got = found(store.get_entry(e.id, owner.id))
    assert got.title == "v2"
    assert found(got.updated_at) >= found(got.created_at)


def test_links_roundtrip_as_uuids(store: PostgresStore, owner: Principal) -> None:
    target = Entry(
        id=new_id(), kind=Kind.DOC, title="target", body="b", owner_id=owner.id
    )
    store.put_entry(target)
    e = Entry(
        id=new_id(),
        kind=Kind.NOTE,
        title="src",
        body="b",
        owner_id=owner.id,
        links=[target.id],
    )
    store.put_entry(e)
    assert found(store.get_entry(e.id, owner.id)).links == [target.id]


def test_set_superseded_marks_the_old_entry(
    store: PostgresStore, owner: Principal
) -> None:
    old = Entry(id=new_id(), kind=Kind.NOTE, title="old", body="b", owner_id=owner.id)
    new = Entry(id=new_id(), kind=Kind.NOTE, title="new", body="b", owner_id=owner.id)
    store.put_entry(old)
    store.put_entry(new)
    assert store.set_superseded(old.id, new.id, owner.id) is True
    assert found(store.get_entry(old.id, owner.id)).superseded_by == new.id


def test_set_superseded_refuses_across_owners(
    store: PostgresStore, owner: Principal
) -> None:
    other = store.ensure_principal("someone-else")
    old = Entry(id=new_id(), kind=Kind.NOTE, title="old", body="b", owner_id=owner.id)
    new = Entry(id=new_id(), kind=Kind.NOTE, title="new", body="b", owner_id=owner.id)
    store.put_entry(old)
    store.put_entry(new)
    assert store.set_superseded(old.id, new.id, other.id) is False


def test_set_superseded_refuses_when_new_entry_belongs_to_another_owner(
    store: PostgresStore, owner: Principal
) -> None:
    other = store.ensure_principal("someone-else")
    old = Entry(id=new_id(), kind=Kind.NOTE, title="old", body="b", owner_id=owner.id)
    foreign_new = Entry(
        id=new_id(), kind=Kind.NOTE, title="new", body="b", owner_id=other.id
    )
    store.put_entry(old)
    store.put_entry(foreign_new)
    assert store.set_superseded(old.id, foreign_new.id, owner.id) is False
    assert found(store.get_entry(old.id, owner.id)).superseded_by is None


def test_put_entry_cannot_overwrite_another_owners_entry(
    store: PostgresStore, owner: Principal
) -> None:
    other = store.ensure_principal("mallory")
    mine = store.put_entry(
        Entry(
            id=new_id(), kind=Kind.NOTE, title="Mine", body="my body", owner_id=owner.id
        )
    )

    with pytest.raises(PermissionError):
        store.put_entry(
            Entry(
                id=mine.id,
                kind=Kind.NOTE,
                title="PWNED",
                body="pwned body",
                owner_id=other.id,
            )
        )

    unchanged = found(store.get_entry(mine.id, owner.id))
    assert unchanged.title == "Mine"
    assert unchanged.body == "my body"
    assert unchanged.owner_id == owner.id
