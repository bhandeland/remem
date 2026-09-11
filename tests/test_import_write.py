"""Importing twice must not duplicate. Needs a store."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import psycopg
import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Kind, Origin, Principal, Query
from remem.importers.base import SourceKind, SourceRecord
from remem.services.import_ import ImportPreconditionFailed, run

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


# A real SourceRecord varied with `replace`, for the reason spelled out in
# test_import_mapping.py: a dict of heterogeneous values splatted in makes
# every argument a union and the call uncheckable.
_BASE = SourceRecord(
    source_id="m1",
    kind=SourceKind.OBSERVATION,
    project="at-workspace",
    title="A title",
    summary="A hook",
    body="the original body",
    tags=("cmem-type:discovery",),
)


def _record(**kw) -> SourceRecord:
    return replace(_BASE, **kw)


def _live(store, owner):
    return store.search(
        Query(tags=["cmem:m1"], origins=[Origin.IMPORTED], limit=50), owner.id
    )


def test_an_import_writes_an_entry_with_the_imported_origin(store, owner):
    report = run(store, owner.id, [_record()], namespace="cmem")

    assert report.created == 1
    [hit] = _live(store, owner)
    assert hit.entry.origin is Origin.IMPORTED
    assert hit.entry.kind is Kind.NOTE
    assert hit.entry.summary == "A hook"


def test_importing_the_same_record_twice_writes_nothing_the_second_time(store, owner):
    run(store, owner.id, [_record()], namespace="cmem")

    report = run(store, owner.id, [_record()], namespace="cmem")

    assert report.created == 0
    assert report.unchanged == 1
    assert len(_live(store, owner)) == 1


def test_a_changed_record_supersedes_rather_than_duplicating(store, owner):
    run(store, owner.id, [_record()], namespace="cmem")

    report = run(store, owner.id, [_record(body="an edited body")], namespace="cmem")

    assert report.updated == 1
    [hit] = _live(store, owner)
    assert hit.entry.body == "an edited body"


def test_the_replacement_keeps_the_identity_tag(store, owner):
    """Without this a third import duplicates: the tag is the only thing
    that answers 'have I seen this row before'."""
    run(store, owner.id, [_record()], namespace="cmem")
    run(store, owner.id, [_record(body="an edited body")], namespace="cmem")

    run(store, owner.id, [_record(body="an edited body")], namespace="cmem")

    assert len(_live(store, owner)) == 1


def test_a_second_run_with_a_different_project_does_not_move_the_entry(store, owner):
    """The spec names 'run again after a mapping is corrected' as an
    expected workflow, but `write.supersede` carries the OLD entry's
    project forward - see write.py:163-172 - so a changed body still lands
    under the original project. `--project` only applies when an entry is
    first created; CLAUDE.md's 'Importing claude-mem' section documents
    this. This test pins that the entry genuinely does not move, and the
    next test pins that the report does not claim it did."""
    run(store, owner.id, [_record()], namespace="cmem", project="at-workspace")

    run(
        store,
        owner.id,
        [_record(body="an edited body")],
        namespace="cmem",
        project="rescued",
    )

    [hit] = _live(store, owner)
    assert hit.entry.project == "at-workspace"


def test_a_second_run_with_a_different_project_reports_where_entries_land(store, owner):
    """`by_project` must describe what actually happened, not what
    `--project` asked for - a reader has no other way to tell that nothing
    moved."""
    run(store, owner.id, [_record()], namespace="cmem", project="at-workspace")

    report = run(
        store,
        owner.id,
        [_record(body="an edited body")],
        namespace="cmem",
        project="rescued",
    )

    assert report.updated == 1
    assert report.by_project == {"at-workspace": 1}
    assert "rescued" not in report.by_project


def test_an_unchanged_second_run_also_reports_the_entrys_real_project(store, owner):
    run(store, owner.id, [_record()], namespace="cmem", project="at-workspace")

    report = run(store, owner.id, [_record()], namespace="cmem", project="rescued")

    assert report.unchanged == 1
    assert report.by_project == {"at-workspace": 1}


def test_a_record_with_no_project_lands_in_the_no_project_bucket(store, owner):
    """Legacy prompt groups always carry `project=None` - `_prompts` has no
    column to read one from. Without a bucket for them, `by_project` simply
    omits those records and the columns stop reconciling against the total,
    which is exactly what the real rehearsal's 67-against-70 was."""
    report = run(
        store,
        owner.id,
        [_record(source_id="prompts:sess-a", project=None)],
        namespace="cmem",
        dry_run=True,
    )

    assert report.by_project == {"(no project)": 1}


def test_a_second_live_hit_on_one_identity_tag_is_refused(store, owner, monkeypatch):
    """`MAX_PER_TAG`'s comment claims a guard against a namespace collision
    silently superseding the wrong entry - this pins that the guard is real,
    not just claimed. Simulated by monkeypatching `store.search` to return
    two hits, since provoking a genuine collision would mean writing two
    live entries under one identity tag by hand, which is exactly the
    scenario this guard exists to catch before it can happen for real."""
    from remem.services.import_ import IdentityCollision

    run(store, owner.id, [_record()], namespace="cmem")
    real_search = store.search

    def two_hits(*args, **kwargs):
        hits = real_search(*args, **kwargs)
        return hits + hits

    monkeypatch.setattr(store, "search", two_hits)

    with pytest.raises(IdentityCollision, match="cmem:m1"):
        run(store, owner.id, [_record(body="an edited body")], namespace="cmem")


def test_a_dry_run_writes_nothing(store, owner):
    report = run(store, owner.id, [_record()], namespace="cmem", dry_run=True)

    assert report.dry_run is True
    assert report.created == 1, "a dry run still reports what it would do"
    assert _live(store, owner) == []


def test_a_dry_run_counts_by_kind_and_project(store, owner):
    records = [
        _record(),
        _record(source_id="m2", kind=SourceKind.SUMMARY, project="other"),
    ]

    report = run(store, owner.id, records, namespace="cmem", dry_run=True)

    assert report.by_project == {"at-workspace": 1, "other": 1}
    assert report.by_kind == {"observation": 1, "summary": 1}


def test_a_deleted_source_row_never_removes_an_entry(store, owner):
    """No orphan sweep. The source is being decommissioned - a row that
    stops existing there must not delete knowledge here."""
    run(store, owner.id, [_record()], namespace="cmem")

    run(store, owner.id, [], namespace="cmem")

    assert len(_live(store, owner)) == 1


def test_a_skipped_list_passed_in_arrives_on_the_report(store, owner):
    """The skip channel comes from `read()`, not `run()` - `run()` only has
    to carry it through untouched. Without this test a later refactor could
    drop the parameter or forget to copy it onto the Report and nothing
    would catch it."""
    report = run(store, owner.id, [], namespace="cmem", skipped=["t r1: bad kind"])

    assert report.skipped == ["t r1: bad kind"]


def test_a_schema_without_the_imported_origin_is_refused_by_name(conn, owner):
    """A new enum value cannot be USED in the transaction that added it, so
    a store one migration behind fails at the first write. The message must
    name `remem db up` for the genuine schema-too-old case, and also carry
    the original database error so the user can see what actually happened."""
    conn.execute("alter type entry_origin rename value 'imported' to 'imported_x'")
    store = PostgresStore(conn)

    with pytest.raises(ImportPreconditionFailed, match="remem db up"):
        run(store, owner.id, [_record()], namespace="cmem")


def test_a_non_schema_probe_failure_surfaces_its_own_error(store, owner, monkeypatch):
    """When the probe fails for a reason other than schema, that reason must
    appear in the error message - not be hidden behind 'migration 019'.
    A user staring at a connection fault must see the connection fault, not
    be misdirected to `remem db up`."""
    unrelated_error = RuntimeError("simulated connection lost")

    def failing_search(*args, **kwargs):
        raise unrelated_error

    monkeypatch.setattr(store, "search", failing_search)

    with pytest.raises(ImportPreconditionFailed) as exc_info:
        run(store, owner.id, [_record()], namespace="cmem")

    # The original error text must be visible to the user, not swallowed
    assert "simulated connection lost" in str(exc_info.value)
    # The message still names the likely cause, but not as the only one
    assert "likely cause" in str(exc_info.value)
    assert "migration 019" in str(exc_info.value)
