"""The automatic re-ingest's command surface.

`remem ingest <paths>` stays exactly as it was - it is documented, and it
is what a person types. These three commands are about the designated,
automatic half: what a session start runs with nobody watching.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from remem.backends.postgres.migrate import migrate
from remem.cli import app

pytestmark = pytest.mark.db

runner = CliRunner()


@pytest.fixture
def env(live_dsn, monkeypatch, tmp_path):
    import psycopg

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))
    return live_dsn


class FakeEmbedder:
    name = "fake-2"
    dim = 2

    def embed(self, texts):
        return [[float(len(t)), 1.0] for t in texts]


@pytest.fixture(autouse=True)
def _no_real_embedder(monkeypatch):
    """`run` embeds what it ingested, so these tests reach load_embedder.

    Left alone that builds a real LocalEmbedder - importing fastembed,
    building an ONNX session and possibly downloading ~130MB - which is
    exactly what conftest's `_no_shared_embedder` keeps out of the suite for
    the search path. Same rule, applied where the CLI reaches it.
    """
    monkeypatch.setattr("remem.cli.load_embedder", lambda model: FakeEmbedder())


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real git repository, because the paths resolve against its root."""
    import subprocess

    root = tmp_path / "repo"
    (root / "docs" / "specs").mkdir(parents=True)
    (root / "docs" / "specs" / "alpha.md").write_text("# Alpha\n\nbody\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    monkeypatch.chdir(root)
    return root


def test_designate_reports_what_it_stored(env, repo):
    result = runner.invoke(app, ["reingest", "designate", "docs/specs"])

    assert result.exit_code == 0
    assert "docs/specs" in result.stdout


def test_status_lists_the_designation(env, repo):
    runner.invoke(app, ["reingest", "designate", "docs/specs"])

    result = runner.invoke(app, ["reingest", "status"])

    assert result.exit_code == 0
    assert "docs/specs" in result.stdout


def test_status_says_so_when_nothing_is_designated(env, repo):
    result = runner.invoke(app, ["reingest", "status"])

    assert result.exit_code == 0
    assert "not designated" in result.stdout.lower()


def test_designate_refuses_an_absolute_path_loudly(env, repo):
    """A person typed this, so it is fail-loud - unlike `run`."""
    result = runner.invoke(app, ["reingest", "designate", "/etc"])

    assert result.exit_code == 1
    assert "absolute" in result.stdout.lower() + result.stderr.lower()


def test_run_ingests_the_designated_paths(env, repo):
    runner.invoke(app, ["reingest", "designate", "docs/specs"])

    result = runner.invoke(app, ["reingest", "run"])

    assert result.exit_code == 0
    found = runner.invoke(app, ["search", "Alpha"])
    assert "alpha" in found.stdout.lower()


def test_run_is_silent(env, repo):
    """It is spawned detached from a session start. Output goes nowhere,
    and printing is how a hook contract rots."""
    runner.invoke(app, ["reingest", "designate", "docs/specs"])

    result = runner.invoke(app, ["reingest", "run"])

    assert result.stdout == ""


def test_run_exits_zero_outside_a_repository(env, tmp_path, monkeypatch):
    """Fail-soft: no repo means no root to resolve against, and that is an
    ordinary thing rather than a failure worth a non-zero exit."""
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["reingest", "run"])

    assert result.exit_code == 0


def test_run_exits_zero_with_the_database_down(env, repo, monkeypatch):
    """The strongest form of the fail-soft contract: this is spawned by a
    session start, and a knowledge tool must never be why one goes wrong."""
    monkeypatch.setenv("REMEM_DSN", "postgresql://nobody@127.0.0.1:1/nothing")

    result = runner.invoke(app, ["reingest", "run"])

    assert result.exit_code == 0
    assert result.stdout == ""


def test_status_shows_the_last_run_after_a_run(env, repo):
    runner.invoke(app, ["reingest", "designate", "docs/specs"])
    runner.invoke(app, ["reingest", "run"])

    result = runner.invoke(app, ["reingest", "status"])

    assert result.exit_code == 0
    assert "last run: auto" in result.stdout
    # alpha.md is a single h1 with no sub-headings, which `markdown.split`
    # renders as two chunks - the anchor (titled from the h1) and the h1's
    # own heading chunk - so "2 new", not "1": see test_ingest_reports_counts
    # for the same shape on a file that has a sub-heading too.
    assert "2 new" in result.stdout
    assert "all designated paths present" in result.stdout


def test_status_reports_a_designated_path_missing_on_disk(env, repo):
    runner.invoke(app, ["reingest", "designate", "docs/specs", "docs/gone"])

    result = runner.invoke(app, ["reingest", "status"])

    assert "missing on disk: docs/gone" in result.stdout
    assert f"checked against {repo.resolve()}" in result.stdout


def test_status_for_another_project_says_paths_were_not_checked(env, repo):
    runner.invoke(
        app, ["reingest", "designate", "docs/specs", "--project", "elsewhere"]
    )

    result = runner.invoke(app, ["reingest", "status", "--project", "elsewhere"])

    assert "paths not checked: run from inside elsewhere's repository" in result.stdout


def test_status_json(env, repo):
    runner.invoke(app, ["reingest", "designate", "docs/specs"])
    runner.invoke(app, ["reingest", "run"])

    result = runner.invoke(app, ["reingest", "status", "--json"])

    [entry] = json.loads(result.stdout)
    assert entry["project"] == "repo"
    assert entry["last_run"]["trigger"] == "auto"
    assert entry["check"]["missing"] == []


def test_status_json_for_an_undesignated_project_with_a_manual_run(env, repo):
    """The `status()` fallback: nothing is designated, so `designations` is
    empty and `checked_against` is None - there is no disk check to report."""
    runner.invoke(app, ["ingest", "docs/specs/alpha.md"])

    result = runner.invoke(app, ["reingest", "status", "--json"])

    [entry] = json.loads(result.stdout)
    assert entry["designations"] == []
    assert entry["last_run"]["trigger"] == "manual"
    assert entry["check"]["checked_against"] is None
