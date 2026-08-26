"""Cleanups for findings that were parked during the build.

Each test here pins behaviour that a reviewer flagged but that was deferred at
the time. They are grouped by the finding they close so the reason survives.
"""

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Collection, Entry, Kind, Query, new_id
from remem.services import kb, write
from remem.services.search import MAX_LIMIT, find

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


# --- update() could not clear a project -------------------------------------


def test_update_can_clear_project(store, owner):
    """`None` means "unchanged", so clearing needs an explicit sentinel."""
    e = write.remember(store, owner.id, title="T", body="B", project="alpha")
    cleared = write.update(store, owner.id, e.id, project=write.CLEAR)
    assert cleared.project is None
    assert store.get_entry(e.id, owner.id).project is None


def test_update_none_project_still_means_unchanged(store, owner):
    e = write.remember(store, owner.id, title="T", body="B", project="alpha")
    unchanged = write.update(store, owner.id, e.id, title="T2")
    assert unchanged.project == "alpha"


def test_update_can_clear_tags(store, owner):
    e = write.remember(store, owner.id, title="T", body="B", tags=["a", "b"])
    assert write.update(store, owner.id, e.id, tags=[]).tags == []


# --- link() did not guard self-linking --------------------------------------


def test_link_refuses_to_link_an_entry_to_itself(store, owner):
    e = write.remember(store, owner.id, title="T", body="B")
    with pytest.raises(write.CannotLinkToSelf):
        write.link(store, owner.id, e.id, e.id)
    assert store.get_entry(e.id, owner.id).links == []


# --- no services-layer cross-owner coverage ---------------------------------


def test_update_refuses_another_owners_entry(store, owner):
    other = store.ensure_principal("someone-else")
    e = write.remember(store, owner.id, title="Mine", body="B")
    with pytest.raises(write.EntryNotFound):
        write.update(store, other.id, e.id, title="PWNED")
    assert store.get_entry(e.id, owner.id).title == "Mine"


def test_supersede_refuses_another_owners_entry(store, owner):
    other = store.ensure_principal("someone-else")
    e = write.remember(store, owner.id, title="Mine", body="B")
    with pytest.raises(write.EntryNotFound):
        write.supersede(store, other.id, e.id, title="X", body="Y")


def test_link_refuses_across_owners(store, owner):
    other = store.ensure_principal("someone-else")
    mine = write.remember(store, owner.id, title="Mine", body="B")
    theirs = write.remember(store, other.id, title="Theirs", body="B")
    with pytest.raises(write.EntryNotFound):
        write.link(store, owner.id, mine.id, theirs.id)


# --- the limit-cap test could not fail --------------------------------------


def test_find_actually_caps_the_limit(store, owner):
    """Needs more rows than MAX_LIMIT for the cap to be observable at all."""
    for i in range(MAX_LIMIT + 25):
        write.remember(store, owner.id, title=f"Entry {i}", body="shared body")
    hits = find(store, owner.id, Query(text="shared", limit=100000))
    assert len(hits) == MAX_LIMIT


# --- store.pin returned None, so failure was silent -------------------------


def test_pin_reports_success_and_failure(store, owner):
    collection = kb.create(store, owner.id, slug="s", title="T")
    entry = write.remember(store, owner.id, title="E", body="B")
    assert store.pin(collection.id, entry.id, 0, owner.id) is True

    other = store.ensure_principal("someone-else")
    theirs = write.remember(store, other.id, title="Theirs", body="B")
    assert store.pin(collection.id, theirs.id, 0, owner.id) is False


# --- db status wrote to the database it was only inspecting -----------------


def test_applied_versions_does_not_create_the_tracking_table(conn):
    """Inspecting an unmigrated database must not write to it."""
    from remem.backends.postgres.migrate import applied_versions, pending_versions

    assert applied_versions(conn) == []
    exists = conn.execute(
        "select to_regclass('public.schema_migrations')"
    ).fetchone()[0]
    assert exists is None, "reading migration state created the tracking table"

    assert "001_initial" in pending_versions(conn)
    exists = conn.execute(
        "select to_regclass('public.schema_migrations')"
    ).fetchone()[0]
    assert exists is None
