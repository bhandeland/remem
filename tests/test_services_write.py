import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Kind, Origin, Query
from remem.services import write
from remem.services.search import find

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def test_remember_returns_a_persisted_entry(store, owner):
    e = write.remember(store, owner.id, title="T", body="B", project="remem")
    assert e.id is not None
    assert store.get_entry(e.id, owner.id).title == "T"


def test_remember_defaults_to_a_memory_from_an_agent(store, owner):
    e = write.remember(store, owner.id, title="T", body="B")
    assert e.kind is Kind.NOTE
    assert e.origin is Origin.AGENT


def test_remember_records_provenance(store, owner):
    e = write.remember(
        store,
        owner.id,
        title="T",
        body="B",
        agent="claude-code",
        session_id="sess-9",
    )
    got = store.get_entry(e.id, owner.id)
    assert got.agent == "claude-code"
    assert got.session_id == "sess-9"


def test_update_changes_only_what_is_given(store, owner):
    e = write.remember(store, owner.id, title="T", body="B", tags=["a"])
    updated = write.update(store, owner.id, e.id, body="B2")
    assert updated.title == "T"
    assert updated.body == "B2"
    assert updated.tags == ["a"]


def test_update_raises_for_a_missing_entry(store, owner):
    from remem.domain import new_id

    with pytest.raises(write.EntryNotFound):
        write.update(store, owner.id, new_id(), body="x")


def test_supersede_creates_a_new_entry_and_marks_the_old(store, owner):
    old = write.remember(
        store,
        owner.id,
        title="Fridays",
        body="deploy fridays",
        project="remem",
        tags=["deploys"],
    )
    new = write.supersede(
        store, owner.id, old.id, title="Tuesdays", body="deploy tuesdays"
    )
    assert new.id != old.id
    assert store.get_entry(old.id, owner.id).superseded_by == new.id


def test_supersede_inherits_project_and_tags(store, owner):
    old = write.remember(
        store,
        owner.id,
        title="T",
        body="B",
        summary="Deploy on Fridays only with a rollback plan",
        project="remem",
        tags=["deploys"],
        kind=Kind.RULE,
    )
    new = write.supersede(store, owner.id, old.id, title="T2", body="B2")
    assert new.project == "remem"
    assert new.tags == ["deploys"]
    assert new.kind is Kind.RULE


def test_superseded_entry_disappears_from_search(store, owner):
    old = write.remember(store, owner.id, title="Fridays", body="deploy fridays")
    write.supersede(store, owner.id, old.id, title="Tuesdays", body="deploy tuesdays")
    titles = [h.entry.title for h in find(store, owner.id, Query(text="deploy"))]
    assert titles == ["Tuesdays"]


def test_supersede_raises_for_a_missing_entry(store, owner):
    from remem.domain import new_id

    with pytest.raises(write.EntryNotFound):
        write.supersede(store, owner.id, new_id(), title="t", body="b")


def test_link_is_bidirectional_and_deduped(store, owner):
    a = write.remember(store, owner.id, title="A", body="B")
    b = write.remember(store, owner.id, title="B", body="B")
    write.link(store, owner.id, a.id, b.id)
    write.link(store, owner.id, a.id, b.id)
    assert store.get_entry(a.id, owner.id).links == [b.id]
    assert store.get_entry(b.id, owner.id).links == [a.id]


def test_nothing_is_hard_deleted_by_supersede(store, owner):
    old = write.remember(store, owner.id, title="Old", body="B")
    write.supersede(store, owner.id, old.id, title="New", body="B2")
    assert store.get_entry(old.id, owner.id) is not None
