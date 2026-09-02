from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Origin, Query
from remem.services import ingest

pytestmark = pytest.mark.db

DOC = "# Design\n\nlead matter\n\n## Alpha\n\nbody a\n\n## Beta\n\nbody b\n"


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


@pytest.fixture
def doc(tmp_path):
    path = tmp_path / "design.md"
    path.write_text(DOC)
    return path


def _titles(store, owner, path):
    hits = store.search(
        Query(tags=[ingest.src_tag(path)], origins=[Origin.INGESTED],
              limit=ingest.MAX_CHUNKS_PER_FILE),
        owner.id,
    )
    return {h.entry.title for h in hits}


def test_a_renamed_heading_leaves_no_live_orphan(store, owner, doc):
    ingest.ingest_file(store, owner.id, doc, project="remem")
    doc.write_text(DOC.replace("## Alpha", "## Alpha, revisited"))

    report = ingest.ingest_file(store, owner.id, doc, project="remem")

    assert report.created == 1
    assert report.swept == 1
    assert _titles(store, owner, doc) == {
        "design", "design § Alpha, revisited", "design § Beta",
    }


def test_a_swept_orphan_is_superseded_by_the_anchor(store, owner, doc):
    ingest.ingest_file(store, owner.id, doc, project="remem")
    anchor = next(
        h.entry for h in store.search(
            Query(tags=[ingest.src_tag(doc)], origins=[Origin.INGESTED], limit=10),
            owner.id)
        if h.entry.title == "design"
    )
    doc.write_text(DOC.replace("## Alpha\n\nbody a\n\n", ""))

    ingest.ingest_file(store, owner.id, doc, project="remem")

    orphans = store.search(
        Query(tags=[ingest.sec_tag("alpha")], include_superseded=True,
              origins=[Origin.INGESTED], limit=10),
        owner.id,
    )
    assert len(orphans) == 1
    assert orphans[0].entry.superseded_by == anchor.id


def test_the_sweep_does_not_reach_other_files(store, owner, tmp_path, doc):
    other = tmp_path / "other.md"
    other.write_text("# Other\n\nlead\n\n## Gamma\n\nbody g\n")
    ingest.ingest_file(store, owner.id, doc, project="remem")
    ingest.ingest_file(store, owner.id, other, project="remem")

    doc.write_text("# Design\n\nlead matter\n")
    ingest.ingest_file(store, owner.id, doc, project="remem")

    assert _titles(store, owner, other) == {"other", "other § Gamma"}


def test_dry_run_sweeps_nothing(store, owner, doc):
    ingest.ingest_file(store, owner.id, doc, project="remem")
    doc.write_text(DOC.replace("## Alpha\n\nbody a\n\n", ""))

    report = ingest.ingest_file(store, owner.id, doc, project="remem", dry_run=True)

    assert report.swept == 1
    assert "design § Alpha" in _titles(store, owner, doc)


def test_ingest_paths_globs_a_directory(store, owner, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.md").write_text("# A\n\nlead\n\n## One\n\nbody\n")
    (tmp_path / "sub" / "b.md").write_text("# B\n\nlead\n\n## Two\n\nbody\n")
    (tmp_path / "ignore.txt").write_text("not markdown")

    report = ingest.ingest_paths(store, owner.id, [tmp_path], project="remem")

    assert report.created == 4
    assert report.failures == []


def test_one_unreadable_file_does_not_cost_the_others(store, owner, tmp_path):
    (tmp_path / "good.md").write_text("# Good\n\nlead\n\n## One\n\nbody\n")
    (tmp_path / "bad.md").write_bytes(b"\xff\xfe\x00 not utf-8 \xff")

    report = ingest.ingest_paths(store, owner.id, [tmp_path], project="remem")

    assert report.created == 2
    assert len(report.failures) == 1
    assert report.failures[0][0].name == "bad.md"
