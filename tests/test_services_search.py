import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Query
from remem.services.search import find
from remem.services.write import remember

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def test_find_delegates_to_the_store(store, owner):
    remember(store, owner.id, title="Postgres", body="tune work_mem")
    hits = find(store, owner.id, Query(text="work_mem"))
    assert [h.entry.title for h in hits] == ["Postgres"]


def test_find_caps_an_absurd_limit(store, owner):
    for i in range(3):
        remember(store, owner.id, title=f"E{i}", body="shared")
    hits = find(store, owner.id, Query(text="shared", limit=100000))
    assert len(hits) == 3


def test_find_rejects_a_nonpositive_limit(store, owner):
    remember(store, owner.id, title="E", body="shared")
    assert find(store, owner.id, Query(text="shared", limit=0)) == []
