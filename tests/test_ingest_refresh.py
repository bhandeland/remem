"""What the spawned re-ingest actually does.

`refresh` is the policy the detached session-start job runs: read this
project's designations, resolve them against the git root, ingest, then
embed. It is the automatic counterpart to `remem ingest` + `remem embed`,
and it differs from them in exactly one way - nobody asked for it, so it
degrades where they would fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Origin, Query
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
    assert "one" in _titles(store, owner.id, Origin.INGESTED)


def test_refresh_honours_the_archive_designation(store, owner, root):
    ingest.designate(store, owner.id, "proj", ["docs/specs"])
    ingest.designate(store, owner.id, "proj", ["docs/plans"], archive=True)

    ingest.refresh(store, owner.id, "proj", root,
                   embed_model="fake-2", load_embedder=FakeEmbedder)

    assert "two" in _titles(store, owner.id, Origin.ARCHIVED)
    assert "two" not in _titles(store, owner.id, Origin.INGESTED)


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
