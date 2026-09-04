from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.services import kb, memory

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def test_a_project_starts_undesignated(store, owner):
    assert memory.designation(store, owner.id, "proj") is None


def test_designate_records_the_collection(store, owner):
    kb.create(store, owner.id, slug="proj-memory", title="Memory",
              project="proj")
    memory.designate(store, owner.id, "proj", "proj-memory")
    assert memory.designation(store, owner.id, "proj") == "proj-memory"


def test_designate_refuses_a_collection_that_does_not_exist(store, owner):
    with pytest.raises(kb.CollectionNotFound):
        memory.designate(store, owner.id, "proj", "nope")
    assert memory.designation(store, owner.id, "proj") is None


def test_designate_none_clears_it(store, owner):
    kb.create(store, owner.id, slug="proj-memory", title="Memory",
              project="proj")
    memory.designate(store, owner.id, "proj", "proj-memory")
    memory.designate(store, owner.id, "proj", None)
    assert memory.designation(store, owner.id, "proj") is None


def test_designation_is_per_project(store, owner):
    kb.create(store, owner.id, slug="a-memory", title="A", project="a")
    memory.designate(store, owner.id, "a", "a-memory")
    assert memory.designation(store, owner.id, "b") is None


# --- the working directory a designation was made from -----------------
# The designation is keyed on the project; the memory directory is keyed on
# the absolute working directory. Nothing derived one from the other, so
# `sync` could only ever run from the right directory and `sync --all` was
# not expressible at all. The designation now records where it was made.
def test_designate_records_the_working_directory(store, owner):
    kb.create(store, owner.id, slug="proj-memory", title="Memory",
              project="proj")
    memory.designate(
        store, owner.id, "proj", "proj-memory", working_dir="/w/proj",
    )
    [d] = memory.designations(store, owner.id)
    assert (d.project, d.collection, d.working_dir) == (
        "proj", "proj-memory", "/w/proj",
    )


def test_a_designation_made_before_this_existed_has_no_directory(store, owner):
    # Migration 015 adds the column to rows that already exist, so every
    # designation made before it reads back as None rather than as a guess.
    # `sync --all` has to say so rather than sync the wrong directory.
    kb.create(store, owner.id, slug="proj-memory", title="Memory",
              project="proj")
    memory.designate(store, owner.id, "proj", "proj-memory")
    [d] = memory.designations(store, owner.id)
    assert d.working_dir is None


def test_designations_lists_every_designated_project(store, owner):
    for p in ("a", "b"):
        kb.create(store, owner.id, slug=f"{p}-memory", title=p, project=p)
        memory.designate(
            store, owner.id, p, f"{p}-memory", working_dir=f"/w/{p}",
        )
    assert {d.project for d in memory.designations(store, owner.id)} == {
        "a", "b",
    }


def test_re_designating_updates_the_directory(store, owner):
    # A repository that moved, or a designation first made from the wrong
    # place: re-running designate is the fix, so it must overwrite.
    kb.create(store, owner.id, slug="proj-memory", title="Memory",
              project="proj")
    memory.designate(store, owner.id, "proj", "proj-memory",
                     working_dir="/old")
    memory.designate(store, owner.id, "proj", "proj-memory",
                     working_dir="/new")
    [d] = memory.designations(store, owner.id)
    assert d.working_dir == "/new"
