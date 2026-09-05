"""What the spawned re-ingest actually does.

`refresh` is the policy the detached session-start job runs: read this
project's designations, resolve them against the git root, ingest, then
embed. It is the automatic counterpart to `remem ingest` + `remem embed`,
and it differs from them in exactly one way - nobody asked for it, so it
degrades where they would fail.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import IngestTrigger, Origin, Query
from remem.embed import EmbedderUnavailable
from remem.services import ingest

pytestmark = pytest.mark.db


class FakeEmbedder:
    name = "fake-2"
    dim = 2

    def embed(self, texts):
        return [[float(len(t)), 1.0] for t in texts]


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


@pytest.fixture
def root(tmp_path):
    (tmp_path / "docs" / "specs").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "docs" / "specs" / "one.md").write_text(
        "# One\n\nSpec body.\n\n## Detail\n\nMore.\n"
    )
    (tmp_path / "docs" / "plans" / "two.md").write_text("# Two\n\nPlan body.\n")
    return tmp_path


def _titles(store, owner_id, origin):
    return {
        h.entry.title
        for h in store.search(Query(origins=[origin], limit=50), owner_id)
    }


def test_an_undesignated_project_ingests_nothing(store, owner, root):
    result = ingest.refresh(store, owner.id, "proj", root,
                            embed_model="fake-2", load_embedder=FakeEmbedder)

    assert result.report.created == 0
    assert _titles(store, owner.id, Origin.INGESTED) == set()


def test_an_undesignated_project_never_builds_the_embedder(store, owner, root):
    """The empty case is the common one - it must cost nothing."""
    built = []

    def load():
        built.append(1)
        return FakeEmbedder()

    ingest.refresh(store, owner.id, "proj", root,
                   embed_model="fake-2", load_embedder=load)
    assert built == []


def test_refresh_ingests_the_designated_paths(store, owner, root):
    ingest.designate(store, owner.id, "proj", ["docs/specs"])

    result = ingest.refresh(store, owner.id, "proj", root,
                            embed_model="fake-2", load_embedder=FakeEmbedder)

    assert result.report.created == 2
    assert "One" in _titles(store, owner.id, Origin.INGESTED)


def test_refresh_honours_the_archive_designation(store, owner, root):
    ingest.designate(store, owner.id, "proj", ["docs/specs"])
    ingest.designate(store, owner.id, "proj", ["docs/plans"], archive=True)

    ingest.refresh(store, owner.id, "proj", root,
                   embed_model="fake-2", load_embedder=FakeEmbedder)

    assert "Two" in _titles(store, owner.id, Origin.ARCHIVED)
    assert "Two" not in _titles(store, owner.id, Origin.INGESTED)


def test_refresh_resolves_paths_against_the_given_root(store, owner, root):
    """Stored relative, resolved here - never against the process cwd."""
    ingest.designate(store, owner.id, "proj", ["docs/specs"])

    result = ingest.refresh(store, owner.id, "proj", root,
                            embed_model="fake-2", load_embedder=FakeEmbedder)

    assert result.report.failures == []
    assert result.report.created == 2


def test_refresh_is_idempotent(store, owner, root):
    ingest.designate(store, owner.id, "proj", ["docs/specs"])
    ingest.refresh(store, owner.id, "proj", root,
                   embed_model="fake-2", load_embedder=FakeEmbedder)

    again = ingest.refresh(store, owner.id, "proj", root,
                           embed_model="fake-2", load_embedder=FakeEmbedder)

    assert again.report.created == 0
    assert again.report.unchanged == 2


def test_a_designated_path_that_vanished_is_a_failure_not_a_crash(
    store, owner, root
):
    """A directory renamed since designation must not kill the whole run."""
    ingest.designate(store, owner.id, "proj", ["docs/specs", "docs/gone"])

    result = ingest.refresh(store, owner.id, "proj", root,
                            embed_model="fake-2", load_embedder=FakeEmbedder)

    assert result.report.created == 2
    assert [p.name for p, _ in result.report.failures] == ["gone"]


def test_refresh_embeds_what_it_ingested(store, owner, root):
    ingest.designate(store, owner.id, "proj", ["docs/specs"])

    result = ingest.refresh(store, owner.id, "proj", root,
                            embed_model="fake-2", load_embedder=FakeEmbedder)

    assert result.embedded == 2


def test_refresh_survives_an_unavailable_embedder(store, owner, root):
    """Fail-soft: the ingest still happened, and nobody asked for any of it.

    `remem embed` exits 1 here on purpose - embedding is its whole job. This
    one was spawned by a session start, so losing the semantic tier is worth
    strictly less than the entries it just wrote.
    """
    ingest.designate(store, owner.id, "proj", ["docs/specs"])

    def load():
        raise EmbedderUnavailable("fastembed is not installed")

    result = ingest.refresh(store, owner.id, "proj", root,
                            embed_model="fake-2", load_embedder=load)

    assert result.report.created == 2
    assert result.embedded == 0
    assert result.embed_error is not None


# --- identity must survive the switch from manual to automatic ---------
# The bug this pins: `refresh` resolved designated paths by handing
# `ingest_paths` an ABSOLUTE path, so `src:` came out as
# `src:/Users/.../docs/specs/one.md` where the manual `remem ingest
# docs/specs` had stored `src:docs/specs/one.md`. Nothing matched, nothing
# was superseded, and the first automatic run duplicated the entire corpus -
# 362 chunks - instead of reporting it unchanged.
#
# Identity is the repo-relative path. Reading happens against the root.
# Those are two different jobs and conflating them is what broke it.
def test_refresh_sees_a_manually_ingested_corpus_as_unchanged(
    store, owner, root, monkeypatch
):
    monkeypatch.chdir(root)
    manual = ingest.ingest_paths(
        store, owner.id, [Path("docs/specs")], project="proj",
    )
    assert manual.created == 2

    ingest.designate(store, owner.id, "proj", ["docs/specs"])
    result = ingest.refresh(store, owner.id, "proj", root,
                            embed_model="fake-2", load_embedder=FakeEmbedder)

    assert result.report.created == 0
    assert result.report.unchanged == 2


def test_refresh_stores_repo_relative_source_tags(store, owner, root):
    ingest.designate(store, owner.id, "proj", ["docs/specs"])
    ingest.refresh(store, owner.id, "proj", root,
                   embed_model="fake-2", load_embedder=FakeEmbedder)

    tags = {
        t
        for h in store.search(Query(origins=[Origin.INGESTED], limit=50), owner.id)
        for t in h.entry.tags
        if t.startswith("src:")
    }
    assert tags == {"src:docs/specs/one.md"}
    assert not any(t.startswith("src:/") for t in tags)


def test_refresh_records_a_finished_run_row(store, owner, root):
    ingest.designate(store, owner.id, "proj", ["docs/specs"])
    ingest.designate(store, owner.id, "proj", ["docs/plans"], archive=True)

    ingest.refresh(store, owner.id, "proj", root,
                   embed_model="fake-2", load_embedder=FakeEmbedder)

    run = store.latest_ingest_run(owner.id, "proj")
    assert run is not None
    assert run.trigger is IngestTrigger.AUTO
    assert run.finished_at is not None
    # two chunks from one.md (anchor + "Detail"); two.md has only its
    # opening h1, so markdown.split's headingless branch gives it two
    # chunks too - the anchor plus a duplicate body under its own slug.
    assert run.created == 4
    assert run.embedded == 4
    assert run.failures == []
    assert run.embed_error is None


def test_an_undesignated_project_writes_no_run_row(store, owner, root):
    ingest.refresh(store, owner.id, "proj", root,
                   embed_model="fake-2", load_embedder=FakeEmbedder)

    assert store.latest_ingest_run(owner.id, "proj") is None


def test_a_missing_designated_path_lands_in_the_rows_failures(store, owner, root):
    ingest.designate(store, owner.id, "proj", ["docs/specs", "docs/renamed"])

    ingest.refresh(store, owner.id, "proj", root,
                   embed_model="fake-2", load_embedder=FakeEmbedder)

    run = store.latest_ingest_run(owner.id, "proj")
    assert run.created == 2
    assert [f["path"] for f in run.failures] == ["docs/renamed"]
    assert "No such file" in run.failures[0]["reason"]


def test_an_absent_embedder_is_recorded_not_raised(store, owner, root):
    ingest.designate(store, owner.id, "proj", ["docs/specs"])

    def broken():
        raise EmbedderUnavailable("fastembed is not installed")

    ingest.refresh(store, owner.id, "proj", root,
                   embed_model="fake-2", load_embedder=broken)

    run = store.latest_ingest_run(owner.id, "proj")
    assert run.finished_at is not None
    assert run.created == 2
    assert run.embed_error == "fastembed is not installed"


def test_an_exception_mid_run_is_recorded_and_re_raised(store, owner, root, monkeypatch):
    ingest.designate(store, owner.id, "proj", ["docs/specs"])

    def explode(*a, **kw):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(ingest, "ingest_paths", explode)

    with pytest.raises(RuntimeError):
        ingest.refresh(store, owner.id, "proj", root,
                       embed_model="fake-2", load_embedder=FakeEmbedder)

    run = store.latest_ingest_run(owner.id, "proj")
    assert run.finished_at is not None
    assert run.failures == [{"path": "*", "reason": "RuntimeError: disk on fire"}]


def test_ingest_manual_records_a_manual_row(store, owner, root):
    report = ingest.ingest_manual(
        store, owner.id, [Path("docs/specs")], project="proj", root=root,
    )

    run = store.latest_ingest_run(owner.id, "proj")
    assert report.created == 2
    assert run.trigger is IngestTrigger.MANUAL
    assert run.created == 2
    assert run.embedded == 0


def test_ingest_manual_writes_no_row_for_a_dry_run_or_no_project(store, owner, root):
    ingest.ingest_manual(store, owner.id, [Path("docs/specs")],
                         project="proj", root=root, dry_run=True)
    ingest.ingest_manual(store, owner.id, [Path("docs/specs")],
                         project=None, root=root)

    assert store.latest_ingest_run(owner.id, "proj") is None


# --- atomicity under autocommit ---------------------------------------
#
# `reingest run` opens its session with autocommit=True (migration 017's
# docstring, and the comment at cli.py's session-open call, both explain
# why: the run row must commit before any file is read so a killed process
# leaves "started, unfinished" rather than "never ran"). But `write.supersede`
# is two statements - remember() inserts the replacement, set_superseded()
# retires the old row - and under autocommit those are two transactions,
# not one. A process killed between them used to leave both rows live
# under the same (src:, sec:) pair, forever: the loser is not in
# ingest_file's `existing` dict (keyed one-per-slug) so no later run's
# sweep ever reaches it. This needs a real autocommit connection, not the
# `conn`/`store` fixtures above (a rolled-back transaction), so it uses
# `live_dsn` and a second, independent PostgresStore directly.


def test_a_changed_chunk_is_superseded_atomically_under_autocommit(
    live_dsn, tmp_path, monkeypatch
):
    with psycopg.connect(live_dsn, autocommit=True) as conn:
        migrate(conn)
        store = PostgresStore(conn)
        owner = store.ensure_principal("atomic-ingest-test")

        doc = tmp_path / "a.md"
        doc.write_text("# One\n\nSpec body.\n\n## Detail\n\nFirst.\n")
        ingest.ingest_file(store, owner.id, doc, project="proj")

        def _live(dsn_store):
            return dsn_store.search(
                Query(tags=[ingest.src_tag(doc)],
                      origins=[Origin.INGESTED, Origin.ARCHIVED], limit=50),
                owner.id,
            )

        before = _live(store)
        assert len(before) == 2  # anchor + the "Detail" section

        # Edit the "Detail" section's body so re-ingesting it takes the
        # CHANGED branch - the one that calls write.supersede - rather than
        # the created or unchanged branches, which are each one statement
        # and were never at risk.
        doc.write_text("# One\n\nSpec body.\n\n## Detail\n\nChanged.\n")

        def killed(*a, **kw):
            raise RuntimeError("killed")

        monkeypatch.setattr(store, "set_superseded", killed)

        # ingest_paths catches only OSError/UnicodeDecodeError/TooManyChunks,
        # so the simulated crash propagates - the same as a process actually
        # dying here, just synchronous and inside this test instead of an
        # untimed kill -9.
        with pytest.raises(RuntimeError, match="killed"):
            ingest.ingest_paths(store, owner.id, [doc], project="proj")

        after = _live(store)
        # Watched failing first: without the `with store.transaction():`
        # wrap in ingest_paths, remember()'s insert of the replacement
        # commits on its own (autocommit) before set_superseded ever runs,
        # so `after` has 3 live rows - the original "Detail" plus its
        # never-superseded replacement, both live under the same sec: tag,
        # exactly the permanently-unsweepable duplicate the fix closes.
        assert len(after) == len(before)
        secs = [ingest._sec_of(h.entry) for h in after]
        assert len(secs) == len(set(secs)), (
            "two live chunks share a sec: tag - the replacement insert "
            "was not rolled back with the failed supersede"
        )
