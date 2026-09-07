"""The dedupe CLI: what it prints, and what it exits."""

from __future__ import annotations

import json

import psycopg
import pytest
from typer.testing import CliRunner

from remem.backends.postgres.migrate import migrate
from remem.cli import app

pytestmark = pytest.mark.db

runner = CliRunner()


@pytest.fixture
def env(live_dsn, monkeypatch, tmp_path):
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))
    return live_dsn


def _write(title, body):
    r = runner.invoke(app, ["remember", title, "--body", body])
    assert r.exit_code == 0, r.stdout
    s = runner.invoke(app, ["search", title, "--json"])
    return json.loads(s.stdout)[0]["id"]


def test_report_exits_zero_when_it_finds_duplicates(env):
    """Finding them is the normal condition, not an error."""
    _write("alpha", "same body")
    _write("beta", "same body")

    result = runner.invoke(app, ["dedupe", "report"])

    assert result.exit_code == 0, result.stdout
    assert "Exact duplicates: 1 groups" in result.stdout
    assert "remem dedupe resolve" in result.stdout


def test_report_says_so_when_there_is_nothing(env):
    _write("alpha", "one")

    result = runner.invoke(app, ["dedupe", "report"])

    assert result.exit_code == 0
    assert "Exact duplicates: none." in result.stdout


def test_report_json_carries_both_tiers_and_coverage(env):
    _write("alpha", "same body")
    _write("beta", "same body")

    result = runner.invoke(app, ["dedupe", "report", "--json"])

    payload = json.loads(result.stdout)
    assert len(payload["exact"]) == 1
    assert payload["near"] == []
    assert payload["coverage"] == {"embedded": 0, "total": 2}
    # Explicit, so a machine reader can tell "none found" from "never ran".
    assert payload["near_checked"] is False
    assert payload["threshold"] == 0.95


def test_resolve_supersedes_and_reports_both_titles(env):
    drop = _write("drop me", "same body")
    keep = _write("keep me", "same body")

    result = runner.invoke(app, ["dedupe", "resolve", drop, "--keep", keep])

    assert result.exit_code == 0, result.stdout
    assert drop in result.stdout and keep in result.stdout
    # The report no longer sees a group: one member is superseded.
    after = runner.invoke(app, ["dedupe", "report"])
    assert "Exact duplicates: none." in after.stdout


def test_resolve_refuses_loudly(env):
    e = _write("alpha", "body")

    result = runner.invoke(app, ["dedupe", "resolve", e, "--keep", e])

    assert result.exit_code == 1
    assert "itself" in result.stdout


def test_resolve_refuses_a_malformed_id(env):
    keep = _write("alpha", "body")

    result = runner.invoke(app, ["dedupe", "resolve", "not-a-uuid", "--keep", keep])

    assert result.exit_code == 1
    assert "Cannot resolve" in result.stdout
