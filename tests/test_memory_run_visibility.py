"""The started row is visible to another connection before sync finishes.

This is the test that proves the autocommit change. A same-connection read
would pass under the old single-transaction code and prove nothing.
"""

from __future__ import annotations

import psycopg
import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import MemoryTrigger

pytestmark = pytest.mark.db


def test_a_started_row_is_visible_from_another_connection(live_dsn):
    with psycopg.connect(live_dsn) as setup:
        migrate(setup)
        setup.commit()

    with psycopg.connect(live_dsn, autocommit=True) as writer:
        store = PostgresStore(writer)
        owner = store.ensure_principal("visible")
        run = store.start_memory_run(owner.id, "p", MemoryTrigger.MANUAL)

        # A second connection, while the "run" is still in progress.
        with psycopg.connect(live_dsn) as reader:
            seen = PostgresStore(reader).latest_memory_run(owner.id, "p")

    assert seen is not None and seen.id == run.id
    assert seen.finished_at is None
