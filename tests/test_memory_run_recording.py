"""What sync() records: the dry-run gate, the happy path, and a crash."""

from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import CollectionQuery, MemoryTrigger
from remem.services import kb
from remem.services import memory as memory_service

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


def _designate(store, owner_id, project, directory):
    """Designate `project`, recording `directory` as its working dir.

    The collection has to exist first - `memory.designate` calls `kb.get`
    and raises CollectionNotFound otherwise - and it needs an explicit
    CollectionQuery, because an empty query matches nothing forever. This
    mirrors `_designated` in tests/test_memory_sync.py.
    """
    kb.create(store, owner_id, slug="mem", title="Memory", project=project,
              query=CollectionQuery(project=project))
    memory_service.designate(store, owner_id, project, "mem",
                             working_dir=str(directory))


def test_a_dry_run_records_nothing(store, tmp_path):
    """A dry run changes nothing, so 'last run' must not describe it."""
    owner = store.ensure_principal("run-dry")
    _designate(store, owner.id, "p", tmp_path)

    memory_service.sync(store, owner.id, project="p", directory=tmp_path,
                        dry_run=True)

    assert store.latest_memory_run(owner.id, "p") is None


def test_a_real_run_is_recorded_and_finished(store, tmp_path):
    owner = store.ensure_principal("run-real")
    _designate(store, owner.id, "p", tmp_path)

    memory_service.sync(store, owner.id, project="p", directory=tmp_path)

    run = store.latest_memory_run(owner.id, "p")
    assert run is not None
    assert run.finished_at is not None
    assert run.trigger is MemoryTrigger.MANUAL


def test_the_trigger_is_recorded_as_given(store, tmp_path):
    """'auto' has no caller yet; the column is why this table exists."""
    owner = store.ensure_principal("run-auto")
    _designate(store, owner.id, "p", tmp_path)

    memory_service.sync(store, owner.id, project="p", directory=tmp_path,
                        trigger=MemoryTrigger.AUTO)

    assert store.latest_memory_run(owner.id, "p").trigger is MemoryTrigger.AUTO


def test_an_exception_is_recorded_and_re_raised(store, tmp_path, monkeypatch):
    owner = store.ensure_principal("run-boom")
    _designate(store, owner.id, "p", tmp_path)
    monkeypatch.setattr(
        memory_service, "load_watermarks",
        lambda directory: (_ for _ in ()).throw(OSError("disk gone")),
    )

    with pytest.raises(OSError):
        memory_service.sync(store, owner.id, project="p", directory=tmp_path)

    run = store.latest_memory_run(owner.id, "p")
    assert run.finished_at is not None
    assert run.failures == [{"name": "*", "reason": "disk gone"}]


def test_an_undesignated_project_records_no_run(store, tmp_path):
    owner = store.ensure_principal("run-undesignated")
    with pytest.raises(memory_service.NotDesignated):
        memory_service.sync(store, owner.id, project="p", directory=tmp_path)
    assert store.latest_memory_run(owner.id, "p") is None
