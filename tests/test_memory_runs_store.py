"""The memory_runs reads and writes."""

from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import MemoryTrigger
from remem.store import NotOwner

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


def test_a_started_run_has_no_finish(store):
    owner = store.ensure_principal("runs-start")
    run = store.start_memory_run(owner.id, "p", MemoryTrigger.MANUAL)
    assert run.started_at is not None
    assert run.finished_at is None
    assert run.trigger is MemoryTrigger.MANUAL


def test_finishing_records_every_count_and_list(store):
    owner = store.ensure_principal("runs-finish")
    run = store.start_memory_run(owner.id, "p", MemoryTrigger.MANUAL)

    store.finish_memory_run(
        run.id,
        owner.id,
        adopted=1,
        healed=2,
        edited=3,
        regenerated=4,
        deleted=5,
        unchanged=6,
        renamed=[["old", "new"]],
        conflicts=["c"],
        sidecars=["c"],
        failures=[{"name": "bad", "reason": "boom"}],
    )

    latest = store.latest_memory_run(owner.id, "p")
    assert latest.finished_at is not None
    assert (latest.adopted, latest.healed, latest.edited) == (1, 2, 3)
    assert (latest.regenerated, latest.deleted, latest.unchanged) == (4, 5, 6)
    assert latest.renamed == [["old", "new"]]
    assert latest.conflicts == ["c"] and latest.sidecars == ["c"]
    assert latest.failures == [{"name": "bad", "reason": "boom"}]


def test_latest_is_the_newest_row(store):
    owner = store.ensure_principal("runs-latest")
    store.start_memory_run(owner.id, "p", MemoryTrigger.MANUAL)
    second = store.start_memory_run(owner.id, "p", MemoryTrigger.AUTO)
    assert store.latest_memory_run(owner.id, "p").id == second.id


def test_latest_is_none_when_a_project_never_ran(store):
    owner = store.ensure_principal("runs-none")
    assert store.latest_memory_run(owner.id, "never") is None


def test_runs_are_per_project(store):
    owner = store.ensure_principal("runs-project")
    a = store.start_memory_run(owner.id, "one", MemoryTrigger.MANUAL)
    b = store.start_memory_run(owner.id, "two", MemoryTrigger.MANUAL)
    assert store.latest_memory_run(owner.id, "one").id == a.id
    assert store.latest_memory_run(owner.id, "two").id == b.id


def test_another_principals_run_is_never_returned(store):
    """A fresh fixture guarantees no other rows, which is why one is seeded."""
    mine = store.ensure_principal("runs-mine")
    theirs = store.ensure_principal("runs-theirs")
    store.start_memory_run(theirs.id, "p", MemoryTrigger.MANUAL)

    assert store.latest_memory_run(mine.id, "p") is None


def test_finishing_another_principals_run_is_refused(store):
    mine = store.ensure_principal("finish-mine")
    theirs = store.ensure_principal("finish-theirs")
    run = store.start_memory_run(theirs.id, "p", MemoryTrigger.MANUAL)

    with pytest.raises(NotOwner):
        store.finish_memory_run(
            run.id,
            mine.id,
            adopted=0,
            healed=0,
            edited=0,
            regenerated=0,
            deleted=0,
            unchanged=0,
            renamed=[],
            conflicts=[],
            sidecars=[],
            failures=[],
        )

    assert store.latest_memory_run(theirs.id, "p").finished_at is None
