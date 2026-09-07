import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import CollectionQuery, Origin, Query
from remem.services import handoff, kb, write
from remem.services.search import find

pytestmark = pytest.mark.db

BODY = (
    "## Done\npipeline caching landed\n\n## In flight\n\n## Next steps\n\n## Gotchas\n"
)


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def test_handoffs_are_absent_from_search_by_default(store, owner):
    handoff.write(store, owner.id, project="remem", topic="ci", body=BODY)
    hits = find(store, owner.id, Query(text="pipeline"))
    assert hits == []


def test_handoffs_appear_when_asked_for(store, owner):
    handoff.write(store, owner.id, project="remem", topic="ci", body=BODY)
    hits = find(store, owner.id, Query(text="pipeline"), include_handoffs=True)
    assert [h.entry.origin for h in hits] == [Origin.HANDOFF]


def test_an_explicit_origin_filter_is_never_overridden(store, owner):
    handoff.write(store, owner.id, project="remem", topic="ci", body=BODY)
    hits = find(store, owner.id, Query(text="pipeline", origins=[Origin.HANDOFF]))
    assert len(hits) == 1


def test_ordinary_entries_are_still_found(store, owner):
    write.remember(
        store,
        owner.id,
        title="Pipeline caching",
        body="use the runner cache",
        project="remem",
    )
    assert len(find(store, owner.id, Query(text="pipeline"))) == 1


def test_handoffs_never_reach_a_context_block(store, owner):
    kb.create(
        store,
        owner.id,
        slug="remem",
        title="remem",
        query=CollectionQuery(project="remem"),
    )
    handoff.write(store, owner.id, project="remem", topic="ci", body=BODY)
    write.remember(store, owner.id, title="Real note", body="keep me", project="remem")
    titles = [e.title for e in kb.resolve(store, owner.id, "remem")]
    assert titles == ["Real note"]
