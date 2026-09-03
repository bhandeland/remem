from __future__ import annotations

import pytest

from remem import memory_file
from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import CollectionQuery
from remem.services import kb, memory
from remem.services.write import supersede

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def _designated(store, owner, project="proj", slug="proj-memory"):
    # The query is what makes membership real. kb.create's `project=` only
    # records which project the collection belongs to; without an explicit
    # CollectionQuery the collection matches nothing forever, and the sync
    # does not pin (membership is the query's job, pins are the user's).
    kb.create(
        store,
        owner.id,
        slug=slug,
        title="Memory",
        project=project,
        query=CollectionQuery(project=project),
    )
    memory.designate(store, owner.id, project, slug)
    return slug


def _write_file(directory, name, description, body, type_="project", extra=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        memory_file.render(
            memory_file.MemoryFile(
                name=name, title=name, description=description,
                type=type_, body=body, extra=dict(extra or {}),
            )
        )
    )


def test_an_undesignated_project_writes_nothing(store, owner, tmp_path):
    with pytest.raises(memory.NotDesignated):
        memory.sync(store, owner.id, project="proj", directory=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_case_1_a_stray_file_is_adopted(store, owner, tmp_path):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)
    assert report.adopted == 1
    entries = kb.resolve(store, owner.id, "proj-memory")
    # "A fact", not "a-fact": the title lives in MEMORY.md, there is no
    # MEMORY.md on a first adopt, so parse() falls back to title_from_name.
    assert [e.title for e in entries] == ["A fact"]
    assert entries[0].summary == "a hook"
    assert "mem:a-fact" in entries[0].tags


def test_case_2_a_lost_watermark_heals_when_bodies_match(
    store, owner, tmp_path
):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    (tmp_path / memory.WATERMARK_NAME).unlink()
    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)
    assert report.healed == 1
    assert report.conflicts == []


def test_case_2_a_lost_watermark_conflicts_when_bodies_differ(
    store, owner, tmp_path
):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    (tmp_path / memory.WATERMARK_NAME).unlink()
    _write_file(tmp_path, "a-fact", "a hook", "edited body\n")
    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)
    assert report.conflicts == ["a-fact"]


def test_case_3_an_edited_file_supersedes_the_entry_keeping_its_tag(
    store, owner, tmp_path
):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    _write_file(tmp_path, "a-fact", "a hook", "edited body\n")
    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)
    assert report.edited == 1
    entries = kb.resolve(store, owner.id, "proj-memory")
    assert entries[0].body == "edited body\n"
    assert "mem:a-fact" in entries[0].tags


def test_case_4_an_edited_entry_regenerates_the_file(store, owner, tmp_path):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    entry = kb.resolve(store, owner.id, "proj-memory")[0]
    supersede(store, owner.id, entry.id, title=entry.title, body="from remem\n")
    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)
    assert report.regenerated == 1
    assert "from remem" in (tmp_path / "a-fact.md").read_text()


def test_regenerating_preserves_metadata_remem_does_not_own(
    store, owner, tmp_path
):
    # Claude Code's own bookkeeping lives in these files and an Entry has
    # nowhere to put it, so a regenerate that rendered the entry alone would
    # destroy provenance on every file remem did not write.
    _designated(store, owner)
    _write_file(
        tmp_path, "a-fact", "a hook", "the body\n",
        extra={"originSessionId": "abc123"},
    )
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    entry = kb.resolve(store, owner.id, "proj-memory")[0]
    supersede(store, owner.id, entry.id, title=entry.title, body="from remem\n")

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.regenerated == 1
    text = (tmp_path / "a-fact.md").read_text()
    assert "originSessionId: abc123" in text
    assert "from remem" in text


def test_case_5_both_sides_moved_is_a_conflict_and_writes_nothing(
    store, owner, tmp_path
):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    entry = kb.resolve(store, owner.id, "proj-memory")[0]
    supersede(store, owner.id, entry.id, title=entry.title, body="from remem\n")
    _write_file(tmp_path, "a-fact", "a hook", "from claude\n")

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.conflicts == ["a-fact"]
    assert "from claude" in (tmp_path / "a-fact.md").read_text()
    assert "from remem" in (tmp_path / "a-fact.remem-conflict.md").read_text()


def test_a_conflict_sidecar_is_never_adopted_as_a_memory(
    store, owner, tmp_path
):
    # The sidecar is remem's report of a conflict, not a memory. Adopting it
    # would create a second entry from the same knowledge on the next sync,
    # and then regenerate a file for it forever.
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    entry = kb.resolve(store, owner.id, "proj-memory")[0]
    supersede(store, owner.id, entry.id, title=entry.title, body="from remem\n")
    _write_file(tmp_path, "a-fact", "a hook", "from claude\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.adopted == 0
    names = {
        tag
        for e in kb.resolve(store, owner.id, "proj-memory")
        for tag in e.tags
        if tag.startswith(memory.MEM_TAG_PREFIX)
    }
    assert names == {"mem:a-fact"}


def test_case_6_an_entry_out_of_the_collection_deletes_its_file(
    store, owner, tmp_path
):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    kb.set_query(
        store, owner.id, "proj-memory",
        CollectionQuery(tags=["nothing-matches-this"]),
    )

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.deleted == 1
    assert not (tmp_path / "a-fact.md").exists()


def test_a_file_that_moved_is_never_deleted(store, owner, tmp_path):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    kb.set_query(
        store, owner.id, "proj-memory",
        CollectionQuery(tags=["nothing-matches-this"]),
    )
    _write_file(tmp_path, "a-fact", "a hook", "hand edited\n")

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.deleted == 0
    assert (tmp_path / "a-fact.md").exists()
    assert report.conflicts == ["a-fact"]


def test_a_second_sync_writes_nothing_at_all(store, owner, tmp_path):
    # The property everything else rests on. If this fails, the round-trip
    # is not byte-exact and every sync will churn.
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    before = {
        p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()
    }

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    after = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert after == before
    assert report.unchanged == 1
    assert (report.adopted, report.edited, report.regenerated,
            report.deleted) == (0, 0, 0, 0)


def test_dry_run_reports_without_writing(store, owner, tmp_path):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    report = memory.sync(
        store, owner.id, project="proj", directory=tmp_path, dry_run=True,
    )
    assert report.adopted == 1
    assert kb.resolve(store, owner.id, "proj-memory") == []
    assert not (tmp_path / memory.WATERMARK_NAME).exists()


def test_memory_md_is_written_and_indexes_every_file(store, owner, tmp_path):
    _designated(store, owner)
    _write_file(tmp_path, "b-fact", "second", "b\n")
    _write_file(tmp_path, "a-fact", "first", "a\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    index = (tmp_path / "MEMORY.md").read_text()
    assert index.index("a-fact.md") < index.index("b-fact.md")


def test_a_malformed_file_is_reported_and_costs_nothing_else(
    store, owner, tmp_path
):
    _designated(store, owner)
    _write_file(tmp_path, "good", "a hook", "fine\n")
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "bad.md").write_text("no frontmatter here\n")

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.adopted == 1
    assert [name for name, _ in report.failures] == ["bad"]
