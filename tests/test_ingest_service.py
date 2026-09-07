from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Kind, Origin, Query
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


def _live(store, owner, path):
    return store.search(
        Query(
            tags=[ingest.src_tag(path)],
            origins=[Origin.INGESTED, Origin.ARCHIVED],
            limit=ingest.MAX_CHUNKS_PER_FILE,
        ),
        owner.id,
    )


def test_ingest_writes_an_anchor_and_one_entry_per_heading(store, owner, doc):
    report = ingest.ingest_file(store, owner.id, doc, project="remem")

    assert report.created == 3
    entries = [h.entry for h in _live(store, owner, doc)]
    assert {e.title for e in entries} == {"Design", "Design § Alpha", "Design § Beta"}
    assert all(e.kind is Kind.DOC for e in entries)
    assert all(e.origin is Origin.INGESTED for e in entries)
    assert all(e.project == "remem" for e in entries)


def test_every_chunk_carries_its_src_and_sec_tags(store, owner, doc):
    ingest.ingest_file(store, owner.id, doc, project="remem")

    entries = {h.entry.title: h.entry for h in _live(store, owner, doc)}
    alpha = entries["Design § Alpha"]
    assert ingest.src_tag(doc) in alpha.tags
    assert ingest.sec_tag("alpha") in alpha.tags
    anchor = entries["Design"]
    assert ingest.src_tag(doc) in anchor.tags
    assert not [t for t in anchor.tags if t.startswith("sec:")]


def test_archive_writes_the_archived_origin(store, owner, doc):
    ingest.ingest_file(store, owner.id, doc, project="remem", archive=True)

    assert all(h.entry.origin is Origin.ARCHIVED for h in _live(store, owner, doc))


def test_re_ingesting_an_unchanged_file_writes_nothing(store, owner, doc):
    ingest.ingest_file(store, owner.id, doc, project="remem")
    before = {h.entry.id: h.entry.updated_at for h in _live(store, owner, doc)}

    report = ingest.ingest_file(store, owner.id, doc, project="remem")

    assert (report.created, report.changed, report.unchanged) == (0, 0, 3)
    after = {h.entry.id: h.entry.updated_at for h in _live(store, owner, doc)}
    assert after == before


def test_an_edited_section_supersedes_only_itself(store, owner, doc):
    ingest.ingest_file(store, owner.id, doc, project="remem")
    doc.write_text(DOC.replace("body a", "body a, revised"))

    report = ingest.ingest_file(store, owner.id, doc, project="remem")

    assert (report.created, report.changed, report.unchanged) == (0, 1, 2)
    bodies = {h.entry.title: h.entry.body for h in _live(store, owner, doc)}
    assert "body a, revised" in bodies["Design § Alpha"]
    # The superseded original is kept, not destroyed.
    all_versions = store.search(
        Query(
            tags=[ingest.sec_tag("alpha")],
            include_superseded=True,
            origins=[Origin.INGESTED],
            limit=10,
        ),
        owner.id,
    )
    assert len(all_versions) == 2


def test_a_new_section_is_created_without_touching_its_neighbours(store, owner, doc):
    ingest.ingest_file(store, owner.id, doc, project="remem")
    doc.write_text(DOC + "\n## Gamma\n\nbody g\n")

    report = ingest.ingest_file(store, owner.id, doc, project="remem")

    assert (report.created, report.changed, report.unchanged) == (1, 0, 3)


def test_dry_run_reports_the_plan_and_writes_nothing(store, owner, doc):
    report = ingest.ingest_file(store, owner.id, doc, project="remem", dry_run=True)

    assert report.created == 3
    assert _live(store, owner, doc) == []


def test_too_many_chunks_raises_rather_than_truncating(
    store, owner, tmp_path, monkeypatch
):
    monkeypatch.setattr(ingest, "MAX_CHUNKS_PER_FILE", 3)
    path = tmp_path / "big.md"
    path.write_text(
        "# Big\n\nlead\n\n" + "".join(f"## S{i}\n\nb{i}\n\n" for i in range(5))
    )

    with pytest.raises(ingest.TooManyChunks):
        ingest.ingest_file(store, owner.id, path, project="remem")


def test_a_retitled_document_supersedes_every_chunk_that_carries_the_title(
    store, owner, doc
):
    # The title is part of the embedding text and of the weighted tsvector,
    # so a chunk whose title moved is genuinely found differently and has to
    # supersede. Bodies alone would leave the old titles standing forever:
    # identity is (src, sec), which the h1 does not touch.
    ingest.ingest_file(store, owner.id, doc, project="remem")
    doc.write_text(DOC.replace("# Design", "# Ingest design"))

    report = ingest.ingest_file(store, owner.id, doc, project="remem")

    assert (report.created, report.changed, report.unchanged) == (0, 3, 0)
    titles = {h.entry.title for h in _live(store, owner, doc)}
    assert titles == {"Ingest design", "Ingest design § Alpha", "Ingest design § Beta"}


def test_a_section_title_is_taken_from_the_h1_not_the_filename(store, owner, tmp_path):
    path = tmp_path / "2026-09-01-doc-ingest-design.md"
    path.write_text(DOC)

    ingest.ingest_file(store, owner.id, path, project="remem")

    titles = {h.entry.title for h in _live(store, owner, path)}
    assert titles == {"Design", "Design § Alpha", "Design § Beta"}
