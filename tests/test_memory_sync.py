from __future__ import annotations

import pytest

from remem import memory_file
from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import CollectionQuery, Entry, Kind, Origin, new_id
from remem.services import kb, memory
from remem.services.write import remember, supersede
from tests.conftest import found

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
                name=name,
                title=name,
                description=description,
                type=type_,
                body=body,
                extra=dict(extra or {}),
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


def test_case_2_a_lost_watermark_heals_when_bodies_match(store, owner, tmp_path):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    (tmp_path / memory.WATERMARK_NAME).unlink()
    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)
    assert report.healed == 1
    assert report.conflicts == []


def test_case_2_a_lost_watermark_conflicts_when_bodies_differ(store, owner, tmp_path):
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


def test_syncing_an_edited_summary_less_rule_does_not_raise(store, owner, tmp_path):
    """A Kind.RULE entry that never had a summary - written directly, the
    way the 17 pre-existing rules on this machine got that way, since
    write.remember no longer produces one for a human- or agent-origin
    rule - has no old summary for supersede to carry, so editing its file
    genuinely cannot sync without one. What must not happen is what did
    before this fix: the exception escaping `memory.sync` entirely,
    corrupting every other name's watermark in the same run (final review
    finding 3). It is reported like a malformed file instead, and no
    watermark is written for this name, so the edit is retried - not
    dropped - on the next sync, once `remem update --summary` gives the
    entry something to carry."""
    slug = _designated(store, owner)
    entry = store.put_entry(
        Entry(
            id=new_id(),
            kind=Kind.RULE,
            title="Old rule",
            body="the old case",
            owner_id=owner.id,
            project="proj",
            origin=Origin.HUMAN,
        )
    )

    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    resolved = {e.title: e for e in kb.resolve(store, owner.id, slug)}
    name = next(t for t in resolved[entry.title].tags if t.startswith("mem:"))[
        len("mem:") :
    ]
    _write_file(tmp_path, name, "", "the edited case\n", type_="project")

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.edited == 0
    assert report.conflicts == []
    assert [n for n, _ in report.failures] == [name]
    # Nothing was superseded: the store still holds the original entry,
    # untouched, rather than a half-applied edit.
    entries = kb.resolve(store, owner.id, slug)
    [rule] = [e for e in entries if f"mem:{name}" in e.tags]
    assert rule.body == "the old case"
    assert rule.summary is None


def test_syncing_a_rule_with_its_description_line_deleted_carries_the_old_summary(
    store, owner, tmp_path
):
    """The one-line fix `summary=mf.description or None`: a rule that DOES
    have a summary must not lose it just because the file's edit also
    dropped the `description:` line - memory_file.parse reads a missing
    line back as `""`, not `None`, and passing `""` straight through would
    either overwrite the real summary with nothing or (finding 3) raise
    RuleNeedsSummary mid-sync, since `""` fails the same blank check `None`
    would carry past."""
    slug = _designated(store, owner)
    entry = remember(
        store,
        owner.id,
        title="A real rule",
        body="the case",
        summary="do the thing",
        kind=Kind.RULE,
        project="proj",
        origin=Origin.HUMAN,
    )

    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    resolved = {e.title: e for e in kb.resolve(store, owner.id, slug)}
    name = next(t for t in resolved[entry.title].tags if t.startswith("mem:"))[
        len("mem:") :
    ]
    # The edit drops the description line entirely, same as a user deleting
    # it by hand - and changes the body too, since classify() only compares
    # bodies and a description-only edit would not even reach ADOPT_EDIT.
    _write_file(tmp_path, name, "", "the edited case\n", type_="project")

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.edited == 1
    assert report.failures == []
    entries = kb.resolve(store, owner.id, slug)
    [rule] = [e for e in entries if f"mem:{name}" in e.tags]
    assert rule.body == "the edited case\n"
    assert rule.summary == "do the thing"


def test_regenerating_preserves_metadata_remem_does_not_own(store, owner, tmp_path):
    # Claude Code's own bookkeeping lives in these files and an Entry has
    # nowhere to put it, so a regenerate that rendered the entry alone would
    # destroy provenance on every file remem did not write.
    _designated(store, owner)
    _write_file(
        tmp_path,
        "a-fact",
        "a hook",
        "the body\n",
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


def test_a_conflict_sidecar_is_never_adopted_as_a_memory(store, owner, tmp_path):
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


def test_case_6_an_entry_out_of_the_collection_deletes_its_file(store, owner, tmp_path):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    kb.set_query(
        store,
        owner.id,
        "proj-memory",
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
        store,
        owner.id,
        "proj-memory",
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
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    after = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert after == before
    assert report.unchanged == 1
    assert (report.adopted, report.edited, report.regenerated, report.deleted) == (
        0,
        0,
        0,
        0,
    )


def test_dry_run_reports_without_writing(store, owner, tmp_path):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    report = memory.sync(
        store,
        owner.id,
        project="proj",
        directory=tmp_path,
        dry_run=True,
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


def test_a_malformed_file_with_a_live_entry_is_never_overwritten(
    store, owner, tmp_path
):
    # The dangerous half of "malformed". The entry still exists, so a file
    # treated as absent would be regenerated from the store and the user's
    # text destroyed. Unreadable is not absent.
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    path = tmp_path / "a-fact.md"
    path.write_text("--\nbroken frontmatter\n--\nhand written and precious\n")
    before = path.read_bytes()

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert path.read_bytes() == before
    assert report.regenerated == 0
    assert [name for name, _ in report.failures] == ["a-fact"]


def test_a_malformed_file_is_reported_and_costs_nothing_else(store, owner, tmp_path):
    _designated(store, owner)
    _write_file(tmp_path, "good", "a hook", "fine\n")
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "bad.md").write_text("no frontmatter here\n")

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.adopted == 1
    assert [name for name, _ in report.failures] == ["bad"]


def test_an_unparseable_file_keeps_its_memory_md_line(store, owner, tmp_path):
    # The index is rebuilt from live entries, and a file remem could not parse
    # has none - so without carrying the old line through, "reported and
    # otherwise left completely alone" would still cost the file its index
    # line, and Claude Code would stop loading it.
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    line = (tmp_path / "MEMORY.md").read_text().strip()
    (tmp_path / "a-fact.md").write_text("--\nbroken\n--\nprecious\n")

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert [name for name, _ in report.failures] == ["a-fact"]
    assert (tmp_path / "MEMORY.md").read_text().strip() == line


def test_regenerating_keeps_the_frontmatter_name_the_user_wrote(store, owner, tmp_path):
    # Identity is the filename stem; the frontmatter `name:` is the user's
    # and remem does not own it, so a regenerate must not rewrite it.
    _designated(store, owner)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "a-fact.md").write_text(
        memory_file.render(
            memory_file.MemoryFile(
                name="a-different-slug",
                title="A fact",
                description="a hook",
                type="project",
                body="the body\n",
                extra={},
            )
        )
    )
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    entry = kb.resolve(store, owner.id, "proj-memory")[0]
    supersede(store, owner.id, entry.id, title=entry.title, body="from remem\n")

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.regenerated == 1
    text = (tmp_path / "a-fact.md").read_text()
    assert "name: a-different-slug" in text
    assert "from remem" in text
    # The index still links the file that exists, not the frontmatter slug.
    assert "(a-fact.md)" in (tmp_path / "MEMORY.md").read_text()


def test_a_conflict_that_writes_no_sidecar_says_so(store, owner, tmp_path):
    # A file edited for an entry that has left the collection is reported as
    # a conflict and touches nothing - including writing no sidecar.
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    kb.set_query(
        store,
        owner.id,
        "proj-memory",
        CollectionQuery(tags=["nothing-matches-this"]),
    )
    _write_file(tmp_path, "a-fact", "a hook", "hand edited\n")

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.conflicts == ["a-fact"]
    assert report.sidecars == []
    assert not (tmp_path / f"a-fact{memory.CONFLICT_SUFFIX}").exists()


def test_a_dry_run_conflict_writes_no_sidecar_and_says_so(store, owner, tmp_path):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    entry = kb.resolve(store, owner.id, "proj-memory")[0]
    supersede(store, owner.id, entry.id, title=entry.title, body="from remem\n")
    _write_file(tmp_path, "a-fact", "a hook", "from claude\n")

    report = memory.sync(
        store,
        owner.id,
        project="proj",
        directory=tmp_path,
        dry_run=True,
    )

    assert report.conflicts == ["a-fact"]
    assert report.sidecars == []
    assert not (tmp_path / f"a-fact{memory.CONFLICT_SUFFIX}").exists()


def test_a_collection_at_the_resolve_limit_refuses_to_sync(store, owner, tmp_path):
    # Past kb.RESOLVE_LIMIT an entry remem cannot see is indistinguishable
    # from one that left the collection, and its file would be deleted.
    _designated(store, owner)
    for n in range(kb.RESOLVE_LIMIT):
        remember(
            store,
            owner.id,
            title=f"Fact {n}",
            body=f"body {n}\n",
            kind=Kind.NOTE,
            project="proj",
            origin=Origin.HUMAN,
        )

    with pytest.raises(memory.CollectionTooLarge):
        memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert not (tmp_path / "MEMORY.md").exists()


def test_designating_without_a_project_is_refused(store, owner):
    # Outside a git repository the CLI resolves no project, and the column is
    # not null - so without this the store raises a NotNullViolation
    # traceback out of a command a person typed.
    with pytest.raises(memory.NoProject):
        memory.designate(store, owner.id, None, "proj-memory")


def test_an_entry_with_no_mem_tag_is_exported(store, owner, tmp_path):
    # The store-to-disk direction. Nothing puts a `mem:` tag on an entry
    # written by hand, so an export that only knew about tagged entries would
    # export nothing at all from a collection full of real rules and notes.
    _designated(store, owner)
    remember(
        store,
        owner.id,
        title="Deploys need HTTPS",
        body="use https\n",
        summary="Deploys need HTTPS, not SSH",
        kind=Kind.RULE,
        project="proj",
        origin=Origin.HUMAN,
    )

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.regenerated == 1
    assert (tmp_path / "deploys-need-https.md").read_text().endswith("use https\n")
    assert "deploys-need-https.md" in (tmp_path / "MEMORY.md").read_text()


def test_the_minted_name_persists_so_the_next_sync_is_a_no_op(store, owner, tmp_path):
    # Load-bearing: if the tag were not written back, every sync would mint a
    # fresh name, and a name that moved is a file deleted and rewritten.
    _designated(store, owner)
    remember(
        store,
        owner.id,
        title="Deploys need HTTPS",
        body="use https\n",
        summary="Deploys need HTTPS, not SSH",
        kind=Kind.RULE,
        project="proj",
        origin=Origin.HUMAN,
    )
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.unchanged == 1
    assert report.regenerated == 0
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
    tags = [
        t
        for e in kb.resolve(store, owner.id, "proj-memory")
        for t in e.tags
        if t.startswith(memory.MEM_TAG_PREFIX)
    ]
    assert tags == ["mem:deploys-need-https"]


def test_two_titles_that_slugify_alike_get_two_files(store, owner, tmp_path):
    _designated(store, owner)
    for body in ("first\n", "second\n"):
        remember(
            store,
            owner.id,
            title="Same Title!",
            body=body,
            kind=Kind.NOTE,
            project="proj",
            origin=Origin.HUMAN,
        )

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.regenerated == 2
    written = sorted(p.name for p in tmp_path.glob("*.md") if p.name != "MEMORY.md")
    assert len(written) == 2
    bodies = {(tmp_path / n).read_text().rsplit("---\n", 1)[1] for n in written}
    assert bodies == {"first\n", "second\n"}


def test_a_tagged_entry_with_no_file_keeps_its_name_against_a_minting_collision(
    store, owner, tmp_path
):
    # The seeding-order bug: `taken` starts from the files on disk, and an
    # entry's EXISTING `mem:` name only joins it when the loop reaches that
    # entry. So a tagged entry whose file is absent - deleted, fresh machine,
    # never synced - has not reserved its name yet, and an untagged entry
    # whose title slugifies to the same string can mint it out from under
    # them. The tagged entry is then refused, every run, permanently.
    tagged = remember(
        store,
        owner.id,
        title="Already tagged",
        body="tagged\n",
        project="proj",
        tags=[f"{memory.MEM_TAG_PREFIX}foo"],
    )
    untagged = remember(
        store,
        owner.id,
        title="Foo",
        body="untagged\n",
        project="proj",
    )

    # Untagged first, which is the order that triggers it.
    named = memory._adopt_names(
        store,
        owner.id,
        [untagged, tagged],
        taken=set(),
        report=memory.Report(),
        dry_run=False,
    )

    assert named["foo"].id == tagged.id, "the tagged entry must keep its name"
    assert len(named) == 2, "both entries must be exportable"


# --- sync --all ---------------------------------------------------------
# One invocation for every designated project. The loop lives here rather
# than in the frontend because deciding *which* designations are syncable
# is policy: a row with no recorded working directory, a directory that has
# since been deleted, and a collection that was dropped out from under a
# designation are three different answers, and every frontend has to give
# the same ones.
def _dir_for(cwd):
    return cwd / "memory"


def test_sync_all_syncs_every_designation_that_records_a_directory(
    store, owner, tmp_path
):
    for p in ("a", "b"):
        _designated(store, owner, project=p, slug=f"{p}-memory")
        memory.designate(
            store,
            owner.id,
            p,
            f"{p}-memory",
            working_dir=str(tmp_path / p),
        )
        _write_file(tmp_path / p / "memory", f"{p}-note", "d", "body\n")

    out = memory.sync_all(store, owner.id, resolve_directory=_dir_for)

    assert {o.project for o in out} == {"a", "b"}
    assert all(o.skipped is None for o in out)
    assert [found(o.report).adopted for o in out] == [1, 1]
    # Adopted into the right project's collection, not pooled: the whole
    # point of walking designations is that each has its own directory.
    assert (tmp_path / "a" / "memory" / "a-note.md").exists()
    assert (tmp_path / "b" / "memory" / "b-note.md").exists()


def test_sync_all_skips_a_designation_with_no_recorded_directory(store, owner):
    _designated(store, owner, project="a", slug="a-memory")
    [out] = memory.sync_all(store, owner.id, resolve_directory=_dir_for)
    assert out.report is None
    assert out.skipped is not None
    # Names the fix, because the fix is not obvious: re-designating is what
    # records the directory, and nothing else does.
    assert "designate" in out.skipped


def test_sync_all_reports_a_working_directory_that_is_gone(store, owner, tmp_path):
    _designated(store, owner, project="a", slug="a-memory")
    memory.designate(
        store,
        owner.id,
        "a",
        "a-memory",
        working_dir=str(tmp_path / "gone"),
    )
    [out] = memory.sync_all(store, owner.id, resolve_directory=_dir_for)
    assert out.report is None
    assert "gone" in found(out.skipped) or "exist" in found(out.skipped)


def test_one_project_failing_does_not_stop_the_others(store, owner, tmp_path, conn):
    for p in ("a", "b"):
        _designated(store, owner, project=p, slug=f"{p}-memory")
        memory.designate(
            store,
            owner.id,
            p,
            f"{p}-memory",
            working_dir=str(tmp_path / p),
        )
        _write_file(tmp_path / p / "memory", f"{p}-note", "d", "body\n")
    # A collection dropped out from under its designation - migration 014
    # deliberately has no foreign key, so this is a state that really occurs.
    with conn.cursor() as cur:
        cur.execute("delete from collections where slug = 'a-memory'")

    out = {
        o.project: o
        for o in memory.sync_all(
            store,
            owner.id,
            resolve_directory=_dir_for,
        )
    }

    assert out["a"].report is None and out["a"].skipped is not None
    assert out["b"].report is not None and out["b"].report.adopted == 1


def test_a_renamed_file_moves_its_entry_instead_of_duplicating_it(
    store, owner, tmp_path
):
    # The failure this guards: a rename classified as two independent names -
    # ADOPT_NEW for the new stem, REGENERATE for the old - mints a second
    # entry from the same body and writes the deleted file back out.
    _designated(store, owner)
    _write_file(tmp_path, "old-name", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    before = kb.resolve(store, owner.id, "proj-memory")
    assert len(before) == 1

    (tmp_path / "old-name.md").rename(tmp_path / "new-name.md")
    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    after = kb.resolve(store, owner.id, "proj-memory")
    assert [e.id for e in after] == [before[0].id], "the entry was duplicated"
    assert memory._name_of(after[0]) == "new-name"
    assert report.renamed == [("old-name", "new-name")]
    assert report.adopted == 0
    assert report.regenerated == 0
    assert not (tmp_path / "old-name.md").exists(), "the deleted file came back"
    assert (tmp_path / "new-name.md").exists()


def test_a_renamed_file_leaves_a_watermark_under_its_new_name(store, owner, tmp_path):
    # Without moving the watermark the next sync sees a file with no mark,
    # which is ADOPT_EDIT or CONFLICT depending on the body - so the rename
    # would cost the very gate that makes "which side moved" answerable.
    _designated(store, owner)
    _write_file(tmp_path, "old-name", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    (tmp_path / "old-name.md").rename(tmp_path / "new-name.md")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)

    marks = memory.load_watermarks(tmp_path)
    assert "old-name" not in marks
    assert "new-name" in marks

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)
    assert report.unchanged == 1
    assert report.renamed == []


def test_two_identical_files_matching_one_rename_are_refused_not_guessed(
    store, owner, tmp_path
):
    # Two candidates for the same missing name is genuinely ambiguous. Pick
    # one and the entry follows a coin flip; refuse and the ordinary cases
    # still run, which is how _adopt_names handles two entries sharing a name.
    _designated(store, owner)
    _write_file(tmp_path, "old-name", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)

    (tmp_path / "old-name.md").rename(tmp_path / "one.md")
    (tmp_path / "two.md").write_text((tmp_path / "one.md").read_text())
    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.renamed == []
    assert [name for name, _ in report.failures] == ["old-name"]
    assert "one" in report.failures[0][1] and "two" in report.failures[0][1]


def test_a_dry_run_reports_a_rename_without_writing_it(store, owner, tmp_path):
    _designated(store, owner)
    _write_file(tmp_path, "old-name", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    (tmp_path / "old-name.md").rename(tmp_path / "new-name.md")

    report = memory.sync(
        store, owner.id, project="proj", directory=tmp_path, dry_run=True
    )
    assert report.renamed == [("old-name", "new-name")]
    entries = kb.resolve(store, owner.id, "proj-memory")
    assert memory._name_of(entries[0]) == "old-name", "the tag was rewritten"
    assert memory.load_watermarks(tmp_path).keys() == {"old-name"}


def test_a_rename_with_an_edit_in_the_same_interval_is_not_followed(
    store, owner, tmp_path
):
    # The honest floor, asserted so it is a decision rather than a surprise:
    # the proof of a rename is that the new file is byte-identical to what
    # remem last wrote under the old name. Edit it too and that proof is gone,
    # so this still duplicates.
    _designated(store, owner)
    _write_file(tmp_path, "old-name", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)

    (tmp_path / "old-name.md").rename(tmp_path / "new-name.md")
    _write_file(tmp_path, "new-name", "a hook", "a different body\n")
    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.renamed == []
    assert report.adopted == 1
    assert len(kb.resolve(store, owner.id, "proj-memory")) == 2


def test_two_entries_matching_one_new_file_are_refused_not_guessed(
    store, owner, tmp_path
):
    # The mirror of the two-files case: two watermarks holding the same body,
    # one arriving file. Whichever entry were picked, the other would still
    # regenerate its file, so the "rename" would fix nothing and mis-tag one.
    _designated(store, owner)
    _write_file(tmp_path, "first", "a hook", "the body\n")
    _write_file(tmp_path, "second", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    assert len(kb.resolve(store, owner.id, "proj-memory")) == 2

    (tmp_path / "first.md").unlink()
    (tmp_path / "second.md").rename(tmp_path / "merged.md")
    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.renamed == []
    assert sorted(name for name, _ in report.failures) == ["first", "second"]


def test_an_unparseable_file_is_never_read_as_a_rename_away_from_itself(
    store, owner, tmp_path
):
    # An unreadable file is absent from `files` but present on disk. Treated
    # as a missing name it would look renamed to any new file that happens to
    # match its watermark, re-tagging the entry away from a file sitting
    # right there - and the unreadable file would then never be recovered.
    _designated(store, owner)
    _write_file(tmp_path, "old-name", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    original = kb.resolve(store, owner.id, "proj-memory")[0].id

    (tmp_path / "new-name.md").write_text((tmp_path / "old-name.md").read_text())
    (tmp_path / "old-name.md").write_text("---\nnot: [valid\n")
    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.renamed == []
    assert [name for name, _ in report.failures] == ["old-name"]
    # The copy is adopted on its own merits - that is ADOPT_NEW and not this
    # guard's business. What must not happen is the original entry following
    # a rename away from the file still sitting on disk under its own name.
    names = {
        e.id: memory._name_of(e) for e in kb.resolve(store, owner.id, "proj-memory")
    }
    assert names[original] == "old-name"
    assert (tmp_path / "old-name.md").read_text() == "---\nnot: [valid\n"
