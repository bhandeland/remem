from __future__ import annotations

import pytest
from typer.testing import CliRunner

from remem.backends.postgres.migrate import migrate
from remem.cli import app

pytestmark = pytest.mark.db

runner = CliRunner()


@pytest.fixture
def env(live_dsn, monkeypatch, tmp_path):
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
def docs(tmp_path):
    # The chunk title comes from the h1, not the filename stem, so this file
    # produces "Alpha doc" and "Alpha doc § One".
    (tmp_path / "alpha-doc.md").write_text("# Alpha doc\n\nlead\n\n## One\n\nbody one\n")
    return tmp_path


def test_ingest_reports_counts(env, docs):
    result = runner.invoke(app, ["ingest", str(docs), "--project", "remem"])

    assert result.exit_code == 0
    assert "2 new" in result.stdout


def test_dry_run_says_so_and_writes_nothing(env, docs):
    runner.invoke(app, ["ingest", str(docs), "--project", "remem", "--dry-run"])

    result = runner.invoke(app, ["search", "body one", "--project", "remem"])
    assert "No matches." in result.stdout


def test_dry_run_does_not_tell_you_to_embed(env, docs):
    result = runner.invoke(app, ["ingest", str(docs), "--project", "remem", "--dry-run"])

    assert "Would write" in result.stdout
    assert "remem embed" not in result.stdout


def test_archived_chunks_are_hidden_until_asked_for(env, docs):
    runner.invoke(app, ["ingest", str(docs), "--project", "remem", "--archive"])

    hidden = runner.invoke(app, ["search", "body one", "--project", "remem"])
    assert "No matches." in hidden.stdout

    shown = runner.invoke(app, ["search", "body one", "--project", "remem", "--archived"])
    assert "Alpha doc § One" in shown.stdout


def test_a_failure_is_named_and_exits_non_zero(env, docs):
    (docs / "bad.md").write_bytes(b"\xff\xfe\x00 not utf-8 \xff")

    result = runner.invoke(app, ["ingest", str(docs), "--project", "remem"])

    assert result.exit_code == 1
    # Failures go to stderr; stdout carries the counts and nothing else.
    assert "bad.md" in result.stderr
    assert "2 new" in result.stdout  # anchor + one section from good.md
