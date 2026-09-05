"""The two dedupe queries, against a real database.

Vectors are hand-written 2-dimensional ones, as in test_semantic_search.py:
no model runs here, and the expected ordering is verifiable by eye.
"""

from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Entry, Kind, Origin, Query, new_id

pytestmark = pytest.mark.db

MODEL = "test-2d"


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


def _entry(store, owner_id, title, body="shared body", **kw):
    return store.put_entry(Entry(id=new_id(), kind=Kind.NOTE, title=title,
                                 body=body, owner_id=owner_id, **kw))


def test_identical_bodies_group(store):
    owner = store.ensure_principal("dedupe-exact")
    a = _entry(store, owner.id, "first")
    b = _entry(store, owner.id, "second")
    _entry(store, owner.id, "different", body="not the same")

    sets = store.exact_duplicate_groups(Query(limit=50), owner.id)

    assert len(sets) == 1
    assert {e.id for e in sets[0].entries} == {a.id, b.id}


def test_entries_differing_only_in_title_still_group(store):
    """The checksum is over the body alone - see the spec."""
    owner = store.ensure_principal("dedupe-title")
    a = _entry(store, owner.id, "a title")
    b = _entry(store, owner.id, "a completely different title")

    sets = store.exact_duplicate_groups(Query(limit=50), owner.id)

    assert {e.id for e in sets[0].entries} == {a.id, b.id}


def test_a_trailing_newline_is_not_a_different_fact(store):
    owner = store.ensure_principal("dedupe-trim")
    a = _entry(store, owner.id, "a", body="one fact")
    b = _entry(store, owner.id, "b", body="one fact\n")

    sets = store.exact_duplicate_groups(Query(limit=50), owner.id)

    assert {e.id for e in sets[0].entries} == {a.id, b.id}


def test_internal_whitespace_is_a_different_body(store):
    """btrim and nothing looser. The near tier catches these, with a score."""
    owner = store.ensure_principal("dedupe-inner")
    _entry(store, owner.id, "a", body="one  fact")
    _entry(store, owner.id, "b", body="one fact")

    assert store.exact_duplicate_groups(Query(limit=50), owner.id) == []


def test_a_superseded_member_is_excluded(store):
    owner = store.ensure_principal("dedupe-superseded")
    a = _entry(store, owner.id, "old")
    b = _entry(store, owner.id, "new")
    store.set_superseded(a.id, b.id, owner.id)

    assert store.exact_duplicate_groups(Query(limit=50), owner.id) == []


def test_another_principal_identical_bodies_never_appear(store):
    """A fresh fixture guarantees no other rows, which is why one is seeded."""
    mine = store.ensure_principal("dedupe-mine")
    theirs = store.ensure_principal("dedupe-theirs")
    a = _entry(store, mine.id, "mine one")
    b = _entry(store, mine.id, "mine two")
    t1 = _entry(store, theirs.id, "theirs one")
    t2 = _entry(store, theirs.id, "theirs two")

    sets = store.exact_duplicate_groups(Query(limit=50), mine.id)

    found = {e.id for s in sets for e in s.entries}
    assert found == {a.id, b.id}
    assert t1.id not in found and t2.id not in found


def test_filters_narrow_the_population(store):
    owner = store.ensure_principal("dedupe-filter")
    _entry(store, owner.id, "a", project="one")
    _entry(store, owner.id, "b", project="two")

    assert store.exact_duplicate_groups(Query(limit=50), owner.id) != []
    assert store.exact_duplicate_groups(
        Query(project="one", limit=50), owner.id) == []


def test_origins_are_not_filtered_by_default(store):
    """An EXTRACTED twin of a hand-written entry is the point of this report."""
    owner = store.ensure_principal("dedupe-origins")
    a = _entry(store, owner.id, "human one", origin=Origin.HUMAN)
    b = _entry(store, owner.id, "machine one", origin=Origin.EXTRACTED)

    sets = store.exact_duplicate_groups(Query(limit=50), owner.id)

    assert {e.id for e in sets[0].entries} == {a.id, b.id}
