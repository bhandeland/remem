"""Run rows: the after-the-fact record of an ingest.

The refresh is fail-soft and detached, so this table is the only thing on
the machine that can say whether it ran. A started row with no finish is a
statement - the process died - and must read back that way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import IngestTrigger, Kind, Origin, Principal
from saddlebag.services import ingest, write
from saddlebag.store import NotOwner
from tests.conftest import found

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


@pytest.fixture
def other(store: PostgresStore) -> Principal:
    return store.ensure_principal("someone-else")


def test_a_started_row_reads_back_unfinished(
    store: PostgresStore, owner: Principal
) -> None:
    run = store.start_ingest_run(owner.id, "proj", IngestTrigger.AUTO)

    latest = store.latest_ingest_run(owner.id, "proj")
    assert latest is not None
    assert latest.id == run.id
    assert latest.trigger is IngestTrigger.AUTO
    assert latest.started_at is not None
    assert latest.finished_at is None
    assert latest.failures == []


def test_finish_records_counts_failures_twins_and_the_embed_error(
    store: PostgresStore, owner: Principal
) -> None:
    run = store.start_ingest_run(owner.id, "proj", IngestTrigger.MANUAL, archive=True)

    store.finish_ingest_run(
        run.id,
        owner.id,
        created=3,
        changed=1,
        unchanged=40,
        swept=2,
        embedded=4,
        failures=[{"path": "docs/gone", "reason": "No such file"}],
        twins=[{"path": "docs/a.md", "existing": "notes/docs/a.md", "live": 12}],
        embed_error="fastembed is not installed",
    )

    latest = found(store.latest_ingest_run(owner.id, "proj"))
    assert latest.finished_at is not None
    assert latest.archive is True
    assert (
        latest.created,
        latest.changed,
        latest.unchanged,
        latest.swept,
        latest.embedded,
    ) == (3, 1, 40, 2, 4)
    assert latest.failures == [{"path": "docs/gone", "reason": "No such file"}]
    assert latest.twins == [
        {"path": "docs/a.md", "existing": "notes/docs/a.md", "live": 12}
    ]
    assert latest.embed_error == "fastembed is not installed"


def test_latest_is_the_newest_started(store: PostgresStore, owner: Principal) -> None:
    first = store.start_ingest_run(owner.id, "proj", IngestTrigger.AUTO)
    second = store.start_ingest_run(owner.id, "proj", IngestTrigger.AUTO)

    assert found(store.latest_ingest_run(owner.id, "proj")).id == second.id
    assert first.id != second.id


def test_latest_is_per_project_and_per_owner(
    store: PostgresStore, owner: Principal, other: Principal
) -> None:
    store.start_ingest_run(owner.id, "proj", IngestTrigger.AUTO)
    theirs = store.start_ingest_run(other.id, "proj", IngestTrigger.AUTO)

    assert store.latest_ingest_run(owner.id, "other-proj") is None
    assert found(store.latest_ingest_run(other.id, "proj")).id == theirs.id
    assert found(store.latest_ingest_run(owner.id, "proj")).id != theirs.id


def test_finishing_someone_elses_row_raises_not_owner(
    store: PostgresStore, owner: Principal, other: Principal
) -> None:
    theirs = store.start_ingest_run(other.id, "proj", IngestTrigger.AUTO)

    with pytest.raises(NotOwner):
        store.finish_ingest_run(
            theirs.id,
            owner.id,
            created=0,
            changed=0,
            unchanged=0,
            swept=0,
            embedded=0,
            failures=[],
            twins=[],
            embed_error=None,
        )
    assert found(store.latest_ingest_run(other.id, "proj")).finished_at is None


def _ingest(
    store: PostgresStore,
    owner: Principal,
    tmp_path: Path,
    rel: str,
    project: str = "proj",
    archive: bool = False,
) -> ingest.Report:
    """Write a two-chunk document at tmp_path/rel and ingest it with `rel`
    as its identity, the way the refresh does."""
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("# Doc\n\nlead\n\n## One\n\nbody\n")
    return ingest.ingest_file(
        store,
        owner.id,
        Path(rel),
        project=project,
        root=tmp_path,
        archive=archive,
    )


def test_anchors_are_the_entries_with_src_but_no_sec(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    _ingest(store, owner, tmp_path, "docs/a.md")
    _ingest(store, owner, tmp_path, "docs/b.md", archive=True)

    found = store.anchors(owner.id, "proj")

    assert sorted(e.title for e in found) == ["Doc", "Doc"]
    assert {t for e in found for t in e.tags} == {"src:docs/a.md", "src:docs/b.md"}
    assert {e.origin for e in found} == {Origin.INGESTED, Origin.ARCHIVED}


def test_anchors_excludes_superseded_other_projects_and_other_owners(
    store: PostgresStore, owner: Principal, other: Principal, tmp_path: Path
) -> None:
    _ingest(store, owner, tmp_path, "docs/a.md")
    _ingest(store, owner, tmp_path, "docs/elsewhere.md", project="other-proj")
    _ingest(store, other, tmp_path, "docs/theirs.md")
    # A hand-written doc with a src-looking tag but no ingest origin.
    write.remember(
        store,
        owner.id,
        title="Hand",
        body="x",
        kind=Kind.DOC,
        project="proj",
        tags=["src:docs/hand.md"],
    )
    [anchor] = [e for e in store.anchors(owner.id, "proj")]
    theirs = store.anchors(other.id, "proj")

    replacement = write.remember(
        store,
        owner.id,
        title="Doc",
        body="new",
        kind=Kind.DOC,
        project="proj",
        tags=["src:docs/a.md"],
        origin=Origin.INGESTED,
    )
    store.set_superseded(anchor.id, replacement.id, owner.id)

    after = store.anchors(owner.id, "proj")
    assert [e.id for e in after] == [replacement.id]
    assert [e.id for e in theirs] != [] and all(e.owner_id == other.id for e in theirs)
