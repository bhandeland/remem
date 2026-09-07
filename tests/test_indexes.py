"""The partial indexes that encode remem's always-on read filters.

Every entry read constrains `owner_id` and excludes superseded rows. These
indexes put both predicates in the index itself, so the index holds only rows a
query could actually return. Tested because an index that silently stops being
used is indistinguishable from one that was never there.
"""

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Entry, Kind, new_id

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


def _indexdef(conn, name: str) -> str | None:
    row = conn.execute(
        "select indexdef from pg_indexes where indexname = %s", (name,)
    ).fetchone()
    return row[0] if row else None


def test_btree_gin_is_installed(store, conn):
    """Required to mix the scalar owner_id with the tsvector in one GIN index."""
    assert (
        conn.execute(
            "select 1 from pg_extension where extname = 'btree_gin'"
        ).fetchone()
        is not None
    )


def test_search_index_covers_owner_and_excludes_superseded(store, conn):
    definition = _indexdef(conn, "entries_owner_search_live_idx")
    assert definition is not None
    assert "gin" in definition.lower()
    assert "owner_id" in definition
    assert "search" in definition
    assert "superseded_by IS NULL" in definition


def test_recent_index_is_ordered_and_excludes_superseded(store, conn):
    definition = _indexdef(conn, "entries_owner_recent_idx")
    assert definition is not None
    assert "owner_id" in definition
    assert "created_at DESC" in definition
    assert "superseded_by IS NULL" in definition


def test_listing_query_actually_uses_the_ordered_index(store, conn):
    """The planner must satisfy ORDER BY from the index, not by sorting.

    This is the path `remem search` with no query and every `kb resolve` take.
    Without the index the whole owner's corpus is scanned and sorted before
    LIMIT applies.
    """
    owner = store.ensure_principal("brandon")
    for i in range(400):
        store.put_entry(
            Entry(
                id=new_id(),
                kind=Kind.NOTE,
                title=f"Entry {i}",
                body="shared body text for planner statistics",
                owner_id=owner.id,
            )
        )
    conn.execute("analyze entries")

    plan = "\n".join(
        row[0]
        for row in conn.execute(
            "explain select id from entries where owner_id = %s "
            "and superseded_by is null order by created_at desc limit 20",
            (owner.id,),
        ).fetchall()
    )
    assert "entries_owner_recent_idx" in plan, plan
    assert "Sort" not in plan, plan
