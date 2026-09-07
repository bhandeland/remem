"""entry_vectors is derived data, and the schema has to keep it that way.

Losing this table must cost a re-run and nothing else. That is what lets
the embedding model change without a migration: insert new rows, delete old
ones, never rewrite `entries`.
"""

import pytest

from remem.backends.postgres.migrate import migrate

pytestmark = pytest.mark.db


@pytest.fixture
def migrated(conn):
    migrate(conn)
    return conn


def test_vector_extension_is_installed(migrated):
    row = migrated.execute(
        "select 1 from pg_extension where extname = 'vector'"
    ).fetchone()
    assert row is not None


def test_primary_key_is_entry_and_model(migrated):
    row = migrated.execute("""
        select string_agg(a.attname, ',' order by a.attname)
        from pg_index i
        join pg_attribute a on a.attrelid = i.indrelid
                           and a.attnum = any(i.indkey)
        where i.indrelid = 'entry_vectors'::regclass and i.indisprimary
    """).fetchone()
    # One row per (entry, model) - which is what lets two models coexist
    # while a re-embed runs.
    assert row[0] == "entry_id,model"


def test_deleting_an_entry_deletes_its_vectors(migrated):
    from remem.backends.postgres.store import PostgresStore
    from remem.domain import Entry, Kind, new_id

    store = PostgresStore(migrated)
    owner = store.ensure_principal("vec-cascade")
    entry = store.put_entry(
        Entry(id=new_id(), kind=Kind.NOTE, title="t", body="b", owner_id=owner.id)
    )
    migrated.execute(
        "insert into entry_vectors (entry_id, model, dim, vector) "
        "values (%s, 'm', 2, '[0.1,0.2]')",
        (entry.id,),
    )
    migrated.execute("delete from entries where id = %s", (entry.id,))
    left = migrated.execute(
        "select count(*) from entry_vectors where entry_id = %s", (entry.id,)
    ).fetchone()[0]
    assert left == 0


def test_vector_column_has_no_declared_dimension(migrated):
    # Deliberate: see the migration's comment. A declared dimension would
    # commit the schema to one embedding model.
    row = migrated.execute("""
        select format_type(a.atttypid, a.atttypmod)
        from pg_attribute a
        where a.attrelid = 'entry_vectors'::regclass and a.attname = 'vector'
    """).fetchone()
    assert row[0] == "vector"


def test_there_is_no_ann_index_yet(migrated):
    # Asserted, not assumed. If someone adds an HNSW index they must come
    # here and say why the trigger in 006_vectors.sql was reached.
    rows = migrated.execute(
        "select indexname from pg_indexes where tablename = 'entry_vectors'"
    ).fetchall()
    assert [r[0] for r in rows] == ["entry_vectors_pkey"]
