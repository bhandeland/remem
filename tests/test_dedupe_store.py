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
    return store.put_entry(
        Entry(
            id=new_id(), kind=Kind.NOTE, title=title, body=body, owner_id=owner_id, **kw
        )
    )


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
    assert store.exact_duplicate_groups(Query(project="one", limit=50), owner.id) == []


def test_origins_are_not_filtered_by_default(store):
    """An EXTRACTED twin of a hand-written entry is the point of this report."""
    owner = store.ensure_principal("dedupe-origins")
    a = _entry(store, owner.id, "human one", origin=Origin.HUMAN)
    b = _entry(store, owner.id, "machine one", origin=Origin.EXTRACTED)

    sets = store.exact_duplicate_groups(Query(limit=50), owner.id)

    assert {e.id for e in sets[0].entries} == {a.id, b.id}


def _vec(store, entry, xy, owner_id):
    store.put_vector(entry.id, MODEL, 2, list(xy), owner_id)


def test_near_pairs_are_returned_once_with_their_similarity(store):
    owner = store.ensure_principal("dedupe-near")
    a = _entry(store, owner.id, "a", body="one")
    b = _entry(store, owner.id, "b", body="two")
    _vec(store, a, (1.0, 0.0), owner.id)
    _vec(store, b, (1.0, 0.0), owner.id)

    pairs, total = store.near_duplicate_pairs(
        Query(limit=50), owner.id, MODEL, threshold=0.9, limit=10
    )

    assert total == 1
    assert len(pairs) == 1
    assert {pairs[0].a.id, pairs[0].b.id} == {a.id, b.id}
    assert pairs[0].similarity == pytest.approx(1.0)


def test_a_pair_below_the_threshold_is_absent(store):
    owner = store.ensure_principal("dedupe-below")
    a = _entry(store, owner.id, "a", body="one")
    b = _entry(store, owner.id, "b", body="two")
    _vec(store, a, (1.0, 0.0), owner.id)
    _vec(store, b, (0.0, 1.0), owner.id)

    pairs, total = store.near_duplicate_pairs(
        Query(limit=50), owner.id, MODEL, threshold=0.5, limit=10
    )

    assert (pairs, total) == ([], 0)


def test_an_entry_with_no_vector_is_absent_rather_than_an_error(store):
    owner = store.ensure_principal("dedupe-novec")
    a = _entry(store, owner.id, "a", body="one")
    b = _entry(store, owner.id, "b", body="two")
    _vec(store, a, (1.0, 0.0), owner.id)

    pairs, total = store.near_duplicate_pairs(
        Query(limit=50), owner.id, MODEL, threshold=0.1, limit=10
    )

    assert (pairs, total) == ([], 0)
    assert b.id is not None  # b simply never joins


def test_the_total_counts_past_the_limit(store):
    """Truncation must be visible - the renderer says 'showing N of M'."""
    owner = store.ensure_principal("dedupe-limit")
    for i in range(4):
        e = _entry(store, owner.id, f"e{i}", body=f"body {i}")
        _vec(store, e, (1.0, 0.0), owner.id)

    pairs, total = store.near_duplicate_pairs(
        Query(limit=50), owner.id, MODEL, threshold=0.9, limit=2
    )

    assert total == 6  # 4 choose 2
    assert len(pairs) == 2


def test_near_pairs_never_cross_owners(store):
    mine = store.ensure_principal("near-mine")
    theirs = store.ensure_principal("near-theirs")
    a = _entry(store, mine.id, "a", body="one")
    t = _entry(store, theirs.id, "t", body="two")
    _vec(store, a, (1.0, 0.0), mine.id)
    _vec(store, t, (1.0, 0.0), theirs.id)

    pairs, total = store.near_duplicate_pairs(
        Query(limit=50), mine.id, MODEL, threshold=0.1, limit=10
    )

    assert (pairs, total) == ([], 0)


def test_a_superseded_entry_is_not_a_near_duplicate(store):
    owner = store.ensure_principal("near-superseded")
    a = _entry(store, owner.id, "a", body="one")
    b = _entry(store, owner.id, "b", body="two")
    _vec(store, a, (1.0, 0.0), owner.id)
    _vec(store, b, (1.0, 0.0), owner.id)
    store.set_superseded(a.id, b.id, owner.id)

    pairs, total = store.near_duplicate_pairs(
        Query(limit=50), owner.id, MODEL, threshold=0.9, limit=10
    )

    assert (pairs, total) == ([], 0)


def test_coverage_counts_embedded_against_total(store):
    owner = store.ensure_principal("dedupe-coverage")
    a = _entry(store, owner.id, "a", body="one")
    _entry(store, owner.id, "b", body="two")
    _vec(store, a, (1.0, 0.0), owner.id)

    assert store.vector_coverage(Query(limit=50), owner.id, MODEL) == (1, 2)


def test_coverage_is_zero_when_nothing_is_embedded(store):
    owner = store.ensure_principal("dedupe-nocoverage")
    _entry(store, owner.id, "a", body="one")

    assert store.vector_coverage(Query(limit=50), owner.id, MODEL) == (0, 1)


def test_coverage_ignores_another_model(store):
    owner = store.ensure_principal("dedupe-othermodel")
    a = _entry(store, owner.id, "a", body="one")
    store.put_vector(a.id, "some-other-model", 2, [1.0, 0.0], owner.id)

    assert store.vector_coverage(Query(limit=50), owner.id, MODEL) == (0, 1)
