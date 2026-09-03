from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.services import kb, memory

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def test_a_project_starts_undesignated(store, owner):
    assert memory.designation(store, owner.id, "proj") is None


def test_designate_records_the_collection(store, owner):
    kb.create(store, owner.id, slug="proj-memory", title="Memory",
              project="proj")
    memory.designate(store, owner.id, "proj", "proj-memory")
    assert memory.designation(store, owner.id, "proj") == "proj-memory"


def test_designate_refuses_a_collection_that_does_not_exist(store, owner):
    with pytest.raises(kb.CollectionNotFound):
        memory.designate(store, owner.id, "proj", "nope")
    assert memory.designation(store, owner.id, "proj") is None


def test_designate_none_clears_it(store, owner):
    kb.create(store, owner.id, slug="proj-memory", title="Memory",
              project="proj")
    memory.designate(store, owner.id, "proj", "proj-memory")
    memory.designate(store, owner.id, "proj", None)
    assert memory.designation(store, owner.id, "proj") is None


def test_designation_is_per_project(store, owner):
    kb.create(store, owner.id, slug="a-memory", title="A", project="a")
    memory.designate(store, owner.id, "a", "a-memory")
    assert memory.designation(store, owner.id, "b") is None
