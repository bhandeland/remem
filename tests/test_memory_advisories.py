"""One line per unhealthy designated project."""

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
    kb.create(
        store,
        owner_id,
        slug="mem",
        title="Memory",
        project=project,
        query=CollectionQuery(project=project),
    )
    memory_service.designate(
        store, owner_id, project, "mem", working_dir=str(directory)
    )


def _finished(store, owner_id, project, trigger=MemoryTrigger.MANUAL, **kw):
    run = store.start_memory_run(owner_id, project, trigger)
    base = dict(
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
    store.finish_memory_run(run.id, owner_id, **{**base, **kw})
    return run


def test_a_healthy_project_raises_nothing(store, tmp_path):
    owner = store.ensure_principal("adv-healthy")
    _designate(store, owner.id, "p", tmp_path)
    _finished(store, owner.id, "p")

    assert memory_service.advisories(store, owner.id) == []


def test_a_never_synced_project_is_named(store, tmp_path):
    owner = store.ensure_principal("adv-never")
    _designate(store, owner.id, "p", tmp_path)

    lines = memory_service.advisories(store, owner.id)

    assert len(lines) == 1
    assert "never synced" in lines[0]
    assert "p" in lines[0]


def test_an_unfinished_run_is_named(store, tmp_path):
    owner = store.ensure_principal("adv-unfinished")
    _designate(store, owner.id, "p", tmp_path)
    store.start_memory_run(owner.id, "p", MemoryTrigger.MANUAL)

    assert "did not finish" in memory_service.advisories(store, owner.id)[0]


def test_failures_are_named(store, tmp_path):
    owner = store.ensure_principal("adv-failures")
    _designate(store, owner.id, "p", tmp_path)
    _finished(store, owner.id, "p", failures=[{"name": "bad", "reason": "boom"}])

    assert "1 failure" in memory_service.advisories(store, owner.id)[0]


def test_a_sidecar_on_disk_is_named(store, tmp_path):
    """The condition that outlives every run - nothing deletes a sidecar."""
    owner = store.ensure_principal("adv-sidecar")
    _designate(store, owner.id, "p", tmp_path)
    _finished(store, owner.id, "p")
    (tmp_path / "note.remem-conflict.md").write_text("x")

    assert "conflict" in memory_service.advisories(store, owner.id)[0]


def test_a_designation_with_no_directory_is_skipped_by_name(store):
    """Written before migration 015. Named, never guessed at."""
    owner = store.ensure_principal("adv-nodir")
    # Straight to the store: `memory.designate` would need a collection,
    # and this test is about a row that predates the working_dir column.
    store.set_memory_collection(owner.id, "p", "mem", None)

    lines = memory_service.advisories(store, owner.id)

    assert len(lines) == 1
    assert "re-designate" in lines[0].lower()


def test_a_missing_directory_is_not_reported_as_clean(store, tmp_path):
    owner = store.ensure_principal("adv-gone")
    gone = tmp_path / "gone"
    _designate(store, owner.id, "p", gone)
    _finished(store, owner.id, "p")

    assert "does not exist" in memory_service.advisories(store, owner.id)[0]


def test_only_designated_projects_are_swept(store, tmp_path):
    """A run row for an undesignated project never raises a line."""
    owner = store.ensure_principal("adv-undesignated")
    store.start_memory_run(owner.id, "stray", MemoryTrigger.MANUAL)

    assert memory_service.advisories(store, owner.id) == []


def test_an_unfinished_run_names_its_trigger(store, tmp_path):
    """An unattended `refresh` and a typed `sync` leave identical rows, so
    the line has to say which one died - they are debugged differently."""
    owner = store.ensure_principal("adv-unfinished-trigger")
    _designate(store, owner.id, "p", tmp_path)
    store.start_memory_run(owner.id, "p", MemoryTrigger.AUTO)

    assert "auto" in memory_service.advisories(store, owner.id)[0]


def test_failures_name_the_trigger_of_the_run_they_came_from(store, tmp_path):
    owner = store.ensure_principal("adv-failures-trigger")
    _designate(store, owner.id, "p", tmp_path)
    _finished(
        store,
        owner.id,
        "p",
        trigger=MemoryTrigger.AUTO,
        failures=[{"name": "bad", "reason": "boom"}],
    )

    assert "auto" in memory_service.advisories(store, owner.id)[0]


def test_a_sidecar_is_not_attributed_to_the_last_run(store, tmp_path):
    """A sidecar outlives every run - nothing deletes one - so naming the
    last run's trigger beside it would attribute it to a sync that may not
    have written it."""
    owner = store.ensure_principal("adv-sidecar-trigger")
    _designate(store, owner.id, "p", tmp_path)
    _finished(store, owner.id, "p", trigger=MemoryTrigger.AUTO)
    (tmp_path / "note.remem-conflict.md").write_text("x")

    line = memory_service.advisories(store, owner.id)[0]
    assert "conflict" in line
    assert "auto" not in line
