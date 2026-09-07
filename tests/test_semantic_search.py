"""The semantic tier, tested with hand-written vectors.

No model runs here. The question at this layer is whether the SQL ranks by
cosine distance, respects the same filters as every other read, and refuses
to cross owners - none of which depends on the numbers being real embeddings.
Hand-written 2-dimensional vectors make the expected ordering something a
reader can verify by eye.
"""

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Entry, Kind, Match, Query, new_id

pytestmark = pytest.mark.db

MODEL = "test-2d"


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


def _entry(store, owner_id, title, body="body", **kw):
    return store.put_entry(
        Entry(
            id=new_id(), kind=Kind.NOTE, title=title, body=body, owner_id=owner_id, **kw
        )
    )


def test_ranks_by_cosine_similarity(store):
    owner = store.ensure_principal("sem-rank")
    near = _entry(store, owner.id, "near")
    far = _entry(store, owner.id, "far")
    store.put_vector(near.id, MODEL, 2, [1.0, 0.0], owner.id)
    store.put_vector(far.id, MODEL, 2, [0.0, 1.0], owner.id)

    hits = store.semantic_search(
        Query(text="q", limit=10), owner.id, [1.0, 0.0], MODEL, threshold=0.1
    )

    assert [h.entry.id for h in hits] == [near.id]  # far is below threshold
    assert hits[0].match is Match.SEMANTIC
    assert hits[0].rank == pytest.approx(1.0)


def test_threshold_excludes_weak_matches(store):
    owner = store.ensure_principal("sem-threshold")
    entry = _entry(store, owner.id, "orthogonal")
    store.put_vector(entry.id, MODEL, 2, [0.0, 1.0], owner.id)

    hits = store.semantic_search(
        Query(text="q", limit=10), owner.id, [1.0, 0.0], MODEL, threshold=0.5
    )
    assert hits == []


def test_never_crosses_owners(store):
    mine = store.ensure_principal("sem-mine")
    yours = store.ensure_principal("sem-yours")
    theirs = _entry(store, yours.id, "not yours")
    store.put_vector(theirs.id, MODEL, 2, [1.0, 0.0], yours.id)

    hits = store.semantic_search(
        Query(text="q", limit=10), mine.id, [1.0, 0.0], MODEL, threshold=0.1
    )
    assert hits == []


def test_applies_the_same_filters_as_other_tiers(store):
    owner = store.ensure_principal("sem-filters")
    a = _entry(store, owner.id, "in project", project="alpha")
    b = _entry(store, owner.id, "other project", project="beta")
    for e in (a, b):
        store.put_vector(e.id, MODEL, 2, [1.0, 0.0], owner.id)

    hits = store.semantic_search(
        Query(text="q", project="alpha", limit=10),
        owner.id,
        [1.0, 0.0],
        MODEL,
        threshold=0.1,
    )
    assert [h.entry.id for h in hits] == [a.id]


def test_ignores_vectors_from_another_model(store):
    # Two models coexisting is the normal state during a re-embed. A search
    # must see exactly one of them, or the ranking is comparing numbers from
    # different spaces - which produces plausible nonsense rather than an
    # error.
    owner = store.ensure_principal("sem-model")
    entry = _entry(store, owner.id, "old model only")
    store.put_vector(entry.id, "other-model", 2, [1.0, 0.0], owner.id)

    hits = store.semantic_search(
        Query(text="q", limit=10), owner.id, [1.0, 0.0], MODEL, threshold=0.1
    )
    assert hits == []


def test_entries_missing_vectors_lists_only_unembedded(store):
    owner = store.ensure_principal("sem-missing")
    done = _entry(store, owner.id, "already embedded")
    todo = _entry(store, owner.id, "not yet")
    store.put_vector(done.id, MODEL, 2, [1.0, 0.0], owner.id)

    missing = store.entries_missing_vectors(owner.id, MODEL, limit=10)
    assert [e.id for e in missing] == [todo.id]


def test_entries_missing_vectors_is_per_model(store):
    # Changing model makes every entry need work again. That is the point of
    # keying on model, and it is what makes a re-embed a normal operation
    # rather than a migration.
    owner = store.ensure_principal("sem-missing-model")
    entry = _entry(store, owner.id, "embedded by the old model")
    store.put_vector(entry.id, "old-model", 2, [1.0, 0.0], owner.id)

    missing = store.entries_missing_vectors(owner.id, "new-model", limit=10)
    assert [e.id for e in missing] == [entry.id]


def test_put_vector_refuses_another_owners_entry(store):
    from remem.store import NotOwner

    mine = store.ensure_principal("put-mine")
    yours = store.ensure_principal("put-yours")
    theirs = _entry(store, yours.id, "not yours")

    with pytest.raises(NotOwner):
        store.put_vector(theirs.id, MODEL, 2, [1.0, 0.0], mine.id)


def test_put_vector_replaces_the_row_for_the_same_model(store):
    owner = store.ensure_principal("put-replace")
    entry = _entry(store, owner.id, "re-embedded")
    store.put_vector(entry.id, MODEL, 2, [1.0, 0.0], owner.id)
    store.put_vector(entry.id, MODEL, 2, [0.0, 1.0], owner.id)

    rows = store._conn.execute(
        "select vector from entry_vectors where entry_id = %s", (entry.id,)
    ).fetchall()
    assert len(rows) == 1
