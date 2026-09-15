from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import CollectionQuery, Origin, Principal, Query
from saddlebag.services import handoff, kb, write
from saddlebag.services.search import find

pytestmark = pytest.mark.db

BODY = (
    "## Done\npipeline caching landed\n\n## In flight\n\n## Next steps\n\n## Gotchas\n"
)


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


def test_handoffs_are_absent_from_search_by_default(
    store: PostgresStore, owner: Principal
) -> None:
    handoff.write(store, owner.id, project="saddlebag", topic="ci", body=BODY)
    hits = find(store, owner.id, Query(text="pipeline"))
    assert hits == []


def test_handoffs_appear_when_asked_for(store: PostgresStore, owner: Principal) -> None:
    handoff.write(store, owner.id, project="saddlebag", topic="ci", body=BODY)
    hits = find(store, owner.id, Query(text="pipeline"), include_handoffs=True)
    assert [h.entry.origin for h in hits] == [Origin.HANDOFF]


def test_an_explicit_origin_filter_is_never_overridden(
    store: PostgresStore, owner: Principal
) -> None:
    handoff.write(store, owner.id, project="saddlebag", topic="ci", body=BODY)
    hits = find(store, owner.id, Query(text="pipeline", origins=[Origin.HANDOFF]))
    assert len(hits) == 1


def test_ordinary_entries_are_still_found(
    store: PostgresStore, owner: Principal
) -> None:
    write.remember(
        store,
        owner.id,
        title="Pipeline caching",
        body="use the runner cache",
        project="saddlebag",
    )
    assert len(find(store, owner.id, Query(text="pipeline"))) == 1


def test_handoffs_never_reach_a_context_block(
    store: PostgresStore, owner: Principal
) -> None:
    kb.create(
        store,
        owner.id,
        slug="saddlebag",
        title="saddlebag",
        query=CollectionQuery(project="saddlebag"),
    )
    handoff.write(store, owner.id, project="saddlebag", topic="ci", body=BODY)
    write.remember(
        store, owner.id, title="Real note", body="keep me", project="saddlebag"
    )
    titles = [e.title for e in kb.resolve(store, owner.id, "saddlebag")]
    assert titles == ["Real note"]
