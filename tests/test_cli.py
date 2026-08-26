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


def test_whoami_reports_the_handle(env):
    result = runner.invoke(app, ["whoami"])
    assert result.exit_code == 0
    assert "brandon" in result.stdout


def test_remember_then_search_finds_it(env):
    r = runner.invoke(app, ["remember", "Postgres tuning",
                            "--body", "raise work_mem", "--project", "remem"])
    assert r.exit_code == 0, r.stdout

    s = runner.invoke(app, ["search", "work_mem", "--json"])
    assert s.exit_code == 0
    payload = json.loads(s.stdout)
    assert payload[0]["title"] == "Postgres tuning"
    assert payload[0]["project"] == "remem"


def test_remember_reads_the_body_from_stdin(env):
    r = runner.invoke(app, ["remember", "From stdin", "--body", "-"],
                      input="piped body text")
    assert r.exit_code == 0
    s = runner.invoke(app, ["search", "piped", "--json"])
    assert json.loads(s.stdout)[0]["title"] == "From stdin"


def test_search_json_is_a_list_even_when_empty(env):
    s = runner.invoke(app, ["search", "nothing-matches-this", "--json"])
    assert s.exit_code == 0
    assert json.loads(s.stdout) == []


def test_get_prints_the_full_body(env):
    r = runner.invoke(app, ["remember", "T", "--body", "the whole body"])
    s = runner.invoke(app, ["search", "whole", "--json"])
    entry_id = json.loads(s.stdout)[0]["id"]
    g = runner.invoke(app, ["get", entry_id])
    assert "the whole body" in g.stdout


def test_get_with_an_unknown_id_exits_nonzero(env):
    from remem.domain import new_id
    g = runner.invoke(app, ["get", str(new_id())])
    assert g.exit_code != 0


def test_supersede_replaces_and_hides_the_old_entry(env):
    runner.invoke(app, ["remember", "Fridays", "--body", "deploy fridays"])
    s = runner.invoke(app, ["search", "fridays", "--json"])
    old_id = json.loads(s.stdout)[0]["id"]

    runner.invoke(app, ["supersede", old_id, "--title", "Tuesdays",
                        "--body", "deploy tuesdays"])
    again = runner.invoke(app, ["search", "deploy", "--json"])
    titles = [x["title"] for x in json.loads(again.stdout)]
    assert titles == ["Tuesdays"]


def test_kb_new_list_pin_and_show(env):
    runner.invoke(app, ["remember", "A rule", "--body", "always lint",
                        "--kind", "rule"])
    s = runner.invoke(app, ["search", "lint", "--json"])
    entry_id = json.loads(s.stdout)[0]["id"]

    assert runner.invoke(app, ["kb", "new", "core", "--title", "Core"]).exit_code == 0
    listed = runner.invoke(app, ["kb", "list"])
    assert "core" in listed.stdout

    assert runner.invoke(app, ["kb", "pin", "core", entry_id]).exit_code == 0
    shown = runner.invoke(app, ["kb", "show", "core"])
    assert "always lint" in shown.stdout
    assert "## Rules" in shown.stdout


def test_kb_show_with_an_unknown_slug_exits_nonzero(env):
    assert runner.invoke(app, ["kb", "show", "nope"]).exit_code != 0


def test_db_status_reports_applied_migrations(env):
    r = runner.invoke(app, ["db", "status"])
    assert r.exit_code == 0
    assert "001_initial" in r.stdout


def test_search_filters_by_kind(env):
    runner.invoke(app, ["remember", "R", "--body", "shared", "--kind", "rule"])
    runner.invoke(app, ["remember", "M", "--body", "shared"])
    s = runner.invoke(app, ["search", "shared", "--kind", "rule", "--json"])
    assert [x["title"] for x in json.loads(s.stdout)] == ["R"]
