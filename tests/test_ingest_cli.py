from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from remem.backends.postgres.migrate import migrate
from remem.cli import app

pytestmark = pytest.mark.db

runner = CliRunner()


@pytest.fixture
def env(live_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Same bootstrap as tests/test_cli.py: the CLI opens its own session, so
    the schema has to be committed before it connects."""
    import psycopg

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))
    return live_dsn


@pytest.fixture
def docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # The chunk title comes from the h1, not the filename stem, so this file
    # produces "Alpha doc" and "Alpha doc § One".
    #
    # chdir: the test process runs from the remem checkout, and inside a
    # repository `remem ingest` refuses a path outside it. tmp_path is not
    # under any repository, so from here identity is the path as typed.
    (tmp_path / "alpha-doc.md").write_text(
        "# Alpha doc\n\nlead\n\n## One\n\nbody one\n"
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_ingest_reports_counts(env: str, docs: Path) -> None:
    result = runner.invoke(app, ["ingest", str(docs), "--project", "remem"])

    assert result.exit_code == 0
    assert "2 new" in result.stdout


def test_dry_run_says_so_and_writes_nothing(env: str, docs: Path) -> None:
    runner.invoke(app, ["ingest", str(docs), "--project", "remem", "--dry-run"])

    result = runner.invoke(app, ["search", "body one", "--project", "remem"])
    assert "No matches." in result.stdout


def test_dry_run_does_not_tell_you_to_embed(env: str, docs: Path) -> None:
    result = runner.invoke(
        app, ["ingest", str(docs), "--project", "remem", "--dry-run"]
    )

    assert "Would write" in result.stdout
    assert "remem embed" not in result.stdout


def test_archived_chunks_are_hidden_until_asked_for(env: str, docs: Path) -> None:
    runner.invoke(app, ["ingest", str(docs), "--project", "remem", "--archive"])

    hidden = runner.invoke(app, ["search", "body one", "--project", "remem"])
    assert "No matches." in hidden.stdout

    shown = runner.invoke(
        app, ["search", "body one", "--project", "remem", "--archived"]
    )
    assert "Alpha doc § One" in shown.stdout


def test_a_failure_is_named_and_exits_non_zero(env: str, docs: Path) -> None:
    (docs / "bad.md").write_bytes(b"\xff\xfe\x00 not utf-8 \xff")

    result = runner.invoke(app, ["ingest", str(docs), "--project", "remem"])

    assert result.exit_code == 1
    # Failures go to stderr; stdout carries the counts and nothing else.
    assert "bad.md" in result.stderr
    assert "2 new" in result.stdout  # anchor + one section from good.md


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real repository, because identity resolves against its top level."""
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.md").write_text("# A\n\nlead\n\n## One\n\nbody one\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    monkeypatch.chdir(root)
    return root


def test_ingest_from_a_subdirectory_supersedes_rather_than_duplicates(
    env: str, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = runner.invoke(app, ["ingest", "docs/a.md"])
    assert first.exit_code == 0, first.output
    assert "2 new" in first.stdout

    (repo / "docs" / "a.md").write_text("# A\n\nlead\n\n## One\n\nbody two\n")
    monkeypatch.chdir(repo / "docs")
    second = runner.invoke(app, ["ingest", "a.md"])

    assert second.exit_code == 0, second.output
    assert "0 new, 1 changed, 1 unchanged" in second.stdout


def test_ingest_refuses_a_path_outside_the_repository(
    env: str, repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere.md"
    outside.write_text("# E\n\nbody\n")

    result = runner.invoke(app, ["ingest", str(outside)])

    assert result.exit_code == 1
    assert "outside the repository" in result.output


def test_ingest_records_a_manual_run_row(env: str, repo: Path) -> None:
    runner.invoke(app, ["ingest", "docs/a.md"])

    result = runner.invoke(app, ["reingest", "status"])
    assert "last run: manual" in result.stdout
    # Nothing is designated, so nothing was checked - "all present" would
    # describe a disk check that never ran.
    assert "all designated paths present" not in result.stdout


def test_ingest_prints_a_twin_and_still_exits_zero(env: str, repo: Path) -> None:
    (repo / "notes").mkdir()
    (repo / "notes" / "a.md").write_text("# A\n\nlead\n\n## One\n\nbody one\n")
    runner.invoke(app, ["ingest", "notes/a.md"])

    result = runner.invoke(app, ["ingest", "docs/a.md"])

    assert result.exit_code == 0
    assert (
        "twin: docs/a.md is new, but src:notes/a.md has 2 live chunks" in result.output
    )
