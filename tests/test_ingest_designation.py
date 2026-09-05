"""Which paths a project re-ingests, and the rules about naming them.

The designation exists because automatic re-ingest has to answer "which
paths, and which are archive" without a human at the keyboard. It follows
the `memory designate` precedent - opt-in per project, holding a value
rather than a boolean - with one row per (project, archive), because the
refresh incantation is genuinely two invocations with different flags.
"""

from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.services import ingest

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def test_a_project_starts_undesignated(store, owner):
    assert ingest.designations(store, owner.id, "proj") == []


def test_designate_records_the_paths(store, owner):
    ingest.designate(store, owner.id, "proj", ["docs/specs", "docs/notes"])
    [d] = ingest.designations(store, owner.id, "proj")
    assert d.paths == ("docs/specs", "docs/notes")
    assert d.archive is False


def test_the_archive_set_is_a_separate_designation(store, owner):
    """Two rows per project at most, matching the two invocations."""
    ingest.designate(store, owner.id, "proj", ["docs/specs"])
    ingest.designate(store, owner.id, "proj", ["docs/plans"], archive=True)
    got = {d.archive: d.paths for d in ingest.designations(store, owner.id, "proj")}
    assert got == {False: ("docs/specs",), True: ("docs/plans",)}


def test_redesignating_replaces_rather_than_appends(store, owner):
    ingest.designate(store, owner.id, "proj", ["docs/specs"])
    ingest.designate(store, owner.id, "proj", ["docs/other"])
    [d] = ingest.designations(store, owner.id, "proj")
    assert d.paths == ("docs/other",)


def test_designate_none_clears_only_that_half(store, owner):
    ingest.designate(store, owner.id, "proj", ["docs/specs"])
    ingest.designate(store, owner.id, "proj", ["docs/plans"], archive=True)
    ingest.designate(store, owner.id, "proj", None)
    [d] = ingest.designations(store, owner.id, "proj")
    assert d.archive is True


def test_designation_is_per_project(store, owner):
    ingest.designate(store, owner.id, "a", ["docs/specs"])
    assert ingest.designations(store, owner.id, "b") == []


def test_designations_without_a_project_returns_every_one(store, owner):
    ingest.designate(store, owner.id, "a", ["docs/specs"])
    ingest.designate(store, owner.id, "b", ["docs/notes"])
    assert {d.project for d in ingest.designations(store, owner.id)} == {"a", "b"}


# --- paths are repo-relative, and that is enforced ---------------------
# The spawned refresh resolves these against the git root, so an absolute
# path would point at whatever the machine that stored it happened to have,
# and a `..` escape would ingest from outside the repository entirely. Both
# are refused at designate time, where there is a human to tell.
def test_designate_refuses_an_absolute_path(store, owner):
    with pytest.raises(ingest.BadDesignation):
        ingest.designate(store, owner.id, "proj", ["/etc"])
    assert ingest.designations(store, owner.id, "proj") == []


def test_designate_refuses_a_path_escaping_the_repository(store, owner):
    with pytest.raises(ingest.BadDesignation):
        ingest.designate(store, owner.id, "proj", ["../elsewhere"])


def test_designate_refuses_an_empty_path_list(store, owner):
    """Clearing is `None`. An empty list is a mistake worth naming."""
    with pytest.raises(ingest.BadDesignation):
        ingest.designate(store, owner.id, "proj", [])
