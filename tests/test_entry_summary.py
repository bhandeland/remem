from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.services.write import remember, supersede

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def test_summary_round_trips_through_the_store(store, owner):
    entry = remember(
        store,
        owner.id,
        title="T",
        body="B",
        summary="one line",
    )
    read_back = store.get_entry(entry.id, owner.id)
    assert read_back.summary == "one line"


def test_summary_defaults_to_none(store, owner):
    entry = remember(store, owner.id, title="T", body="B")
    assert store.get_entry(entry.id, owner.id).summary is None


def test_supersede_carries_the_summary_when_none_is_given(store, owner):
    old = remember(store, owner.id, title="T", body="B", summary="kept")
    new = supersede(store, owner.id, old.id, title="T2", body="B2")
    assert new.summary == "kept"


def test_supersede_replaces_the_summary_when_one_is_given(store, owner):
    old = remember(store, owner.id, title="T", body="B", summary="kept")
    new = supersede(
        store,
        owner.id,
        old.id,
        title="T2",
        body="B2",
        summary="replaced",
    )
    assert new.summary == "replaced"
