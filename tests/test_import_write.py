"""Importing twice must not duplicate. Needs a store."""

from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Kind, Origin, Query
from remem.importers.base import SourceKind, SourceRecord
from remem.services.import_ import SchemaTooOld, run

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def _record(**kw) -> SourceRecord:
    base = dict(
        source_id="m1",
        kind=SourceKind.OBSERVATION,
        project="at-workspace",
        title="A title",
        summary="A hook",
        body="the original body",
        tags=("cmem-type:discovery",),
    )
    return SourceRecord(**{**base, **kw})


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
    name `remem db up`, not a psycopg type error."""
    conn.execute("alter type entry_origin rename value 'imported' to 'imported_x'")
    store = PostgresStore(conn)

    with pytest.raises(SchemaTooOld, match="remem db up"):
        run(store, owner.id, [_record()], namespace="cmem")
