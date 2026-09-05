"""`resolve` points one existing entry at another, and refuses four ways."""

from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Entry, Kind, new_id
from remem.services.dedupe import CannotResolve, resolve
from remem.store import NotOwner

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


def _entry(store, owner_id, title):
    return store.put_entry(Entry(id=new_id(), kind=Kind.NOTE, title=title,
                                 body=title, owner_id=owner_id))


def test_the_dropped_entry_points_at_the_kept_one(store):
    owner = store.ensure_principal("resolve-happy")
    drop = _entry(store, owner.id, "drop")
    keep = _entry(store, owner.id, "keep")

    dropped, kept = resolve(store, owner.id, drop.id, keep.id)

    assert (dropped.id, kept.id) == (drop.id, keep.id)
    assert store.get_entry(drop.id, owner.id).superseded_by == keep.id
    assert store.get_entry(keep.id, owner.id).superseded_by is None


def test_an_entry_cannot_supersede_itself(store):
    owner = store.ensure_principal("resolve-self")
    e = _entry(store, owner.id, "one")

    with pytest.raises(CannotResolve, match="itself"):
        resolve(store, owner.id, e.id, e.id)


def test_an_already_superseded_entry_is_refused(store):
    """Its chain already has a head; re-pointing rewrites history silently."""
    owner = store.ensure_principal("resolve-dropped")
    a = _entry(store, owner.id, "a")
    b = _entry(store, owner.id, "b")
    c = _entry(store, owner.id, "c")
    store.set_superseded(a.id, b.id, owner.id)

    with pytest.raises(CannotResolve, match="already superseded"):
        resolve(store, owner.id, a.id, c.id)


def test_keeping_a_superseded_entry_is_refused(store):
    """It would point a live entry at a tombstone."""
    owner = store.ensure_principal("resolve-keep-dead")
    a = _entry(store, owner.id, "a")
    b = _entry(store, owner.id, "b")
    c = _entry(store, owner.id, "c")
    store.set_superseded(b.id, c.id, owner.id)

    with pytest.raises(CannotResolve, match="itself superseded"):
        resolve(store, owner.id, a.id, b.id)


def test_an_unknown_id_is_refused(store):
    owner = store.ensure_principal("resolve-unknown")
    keep = _entry(store, owner.id, "keep")

    with pytest.raises(CannotResolve, match="no entry"):
        resolve(store, owner.id, new_id(), keep.id)


def test_an_unknown_keep_id_is_refused(store):
    """Both ids are looked up, not just the one being dropped."""
    owner = store.ensure_principal("resolve-unknown-keep")
    drop = _entry(store, owner.id, "drop")

    with pytest.raises(CannotResolve, match="no entry"):
        resolve(store, owner.id, drop.id, new_id())

    assert store.get_entry(drop.id, owner.id).superseded_by is None


def test_another_principals_entry_is_refused(store):
    mine = store.ensure_principal("resolve-mine")
    theirs = store.ensure_principal("resolve-theirs")
    keep = _entry(store, mine.id, "keep")
    drop = _entry(store, theirs.id, "theirs")

    with pytest.raises((CannotResolve, NotOwner)):
        resolve(store, mine.id, drop.id, keep.id)

    assert store.get_entry(drop.id, theirs.id).superseded_by is None
