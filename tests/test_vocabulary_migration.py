"""The renames, and the one they do not reach.

`ALTER TYPE ... RENAME VALUE` renames an enum label. It does not touch the
string "memory" sitting inside a smart collection's jsonb query, and a
collection whose query matches nothing is this codebase's documented worst
failure - silent, permanent, and indistinguishable from an empty store. So
the interesting test here is not "does the enum say note", it is "does a
collection created BEFORE the migration still resolve to the same entries
after it".
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from remem.backends.postgres.migrate import migration_files
from remem.backends.postgres.store import PostgresStore
from remem.domain import Kind, Origin
from remem.services import kb

pytestmark = pytest.mark.db

BEFORE = "006_vectors"


def apply_through(conn, last_version: str) -> None:
    """Apply migrations up to and including `last_version`.

    The suite's other database tests start from a fully migrated schema.
    This one has to stand in the middle, because the whole question is what
    happens to rows written under the old vocabulary.
    """
    conn.execute(
        "create table if not exists schema_migrations ("
        " version text primary key,"
        " applied_at timestamptz not null default clock_timestamp())"
    )
    for version, sql in migration_files():
        conn.execute(sql)
        conn.execute(
            "insert into schema_migrations (version) values (%s)", (version,)
        )
        if version == last_version:
            return


def apply_rest(conn) -> None:
    done = {
        r[0]
        for r in conn.execute("select version from schema_migrations").fetchall()
    }
    for version, sql in migration_files():
        if version in done:
            continue
        conn.execute(sql)
        conn.execute(
            "insert into schema_migrations (version) values (%s)", (version,)
        )


def _old_world(conn, origin: str = "capture"):
    """A principal, an old-vocabulary entry, and a collection that finds it.

    `origin` defaults to 'capture' for the tests exercising the origin
    rename. The collection-resolution test below passes 'human' instead:
    kb.resolve() deliberately excludes capture/extracted-origin entries
    from context blocks (see CLAUDE.md), which is orthogonal to - and would
    otherwise mask - the thing that test actually checks: that the jsonb
    kinds rewrite survives the migration.
    """
    owner = uuid4()
    conn.execute(
        "insert into principals (id, handle, kind) values (%s, 'pre', 'user')",
        (owner,),
    )
    entry = uuid4()
    conn.execute(
        "insert into entries (id, kind, title, body, owner_id, project, origin)"
        " values (%s, 'memory', 'old', 'body', %s, 'proj', %s)",
        (entry, owner, origin),
    )
    conn.execute(
        "insert into collections (id, slug, title, owner_id, query)"
        " values (%s, 'pre', 'Pre', %s, %s)",
        (uuid4(), owner, json.dumps(
            {"tags": [], "kinds": ["memory"], "project": "proj"}
        )),
    )
    return owner, entry


def test_a_pre_migration_collection_resolves_to_the_same_entries(conn):
    apply_through(conn, BEFORE)
    owner, entry = _old_world(conn, origin="human")

    apply_rest(conn)

    resolved = kb.resolve(PostgresStore(conn), owner, "pre")
    assert [e.id for e in resolved] == [entry]
    assert resolved[0].kind is Kind.NOTE


def test_a_captured_entry_reads_back_as_extracted(conn):
    apply_through(conn, BEFORE)
    owner, entry = _old_world(conn)

    apply_rest(conn)

    stored = PostgresStore(conn).get_entry(entry, owner)
    assert stored is not None
    assert stored.origin is Origin.EXTRACTED


def test_the_opt_in_survives_the_table_rename(conn):
    apply_through(conn, BEFORE)
    owner = uuid4()
    conn.execute(
        "insert into principals (id, handle, kind) values (%s, 'optin', 'user')",
        (owner,),
    )
    conn.execute(
        "insert into capture_settings (owner_id, project, enabled)"
        " values (%s, 'proj', true)",
        (owner,),
    )

    apply_rest(conn)

    store = PostgresStore(conn)
    assert store.record_enabled(owner, "proj") is True
    assert store.enabled_record_projects(owner) == ["proj"]
