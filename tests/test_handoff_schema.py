import pytest

from remem.backends.postgres.migrate import migrate, pending_versions
from remem.backends.postgres.store import PostgresStore
from remem.domain import Entry, Kind, Origin, new_id

pytestmark = pytest.mark.db


def test_the_handoff_origin_round_trips(conn):
    migrate(conn)
    store = PostgresStore(conn)
    owner = store.ensure_principal("brandon")
    entry = store.put_entry(
        Entry(
            id=new_id(),
            kind=Kind.DOC,
            title="Handoff: remem (2026-08-27)",
            body="## Done\n",
            owner_id=owner.id,
            project="remem",
            origin=Origin.HANDOFF,
        )
    )
    assert store.get_entry(entry.id, owner.id).origin == Origin.HANDOFF


def test_the_migration_is_applied_by_migrate(conn):
    migrate(conn)
    assert "005_handoff" not in pending_versions(conn)
