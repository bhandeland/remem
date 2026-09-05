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
                        "--summary", "Run lint", "--kind", "rule"])
    s = runner.invoke(app, ["search", "lint", "--json"])
    entry_id = json.loads(s.stdout)[0]["id"]

    assert runner.invoke(app, ["kb", "new", "core", "--title", "Core"]).exit_code == 0
    listed = runner.invoke(app, ["kb", "list"])
    assert "core" in listed.stdout

    assert runner.invoke(app, ["kb", "pin", "core", entry_id]).exit_code == 0
    shown = runner.invoke(app, ["kb", "show", "core"])
    assert "Run lint" in shown.stdout
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


def test_kb_pin_with_an_unknown_entry_exits_cleanly(env):
    from remem.domain import new_id

    runner.invoke(app, ["kb", "new", "core", "--title", "Core"])
    r = runner.invoke(app, ["kb", "pin", "core", str(new_id())])
    assert r.exit_code != 0
    assert "No entry" in r.stderr
    assert "Traceback" not in r.stdout + r.stderr
    assert r.exception is None or isinstance(r.exception, SystemExit)


def test_get_with_a_malformed_id_exits_cleanly(env):
    r = runner.invoke(app, ["get", "abc"])
    assert r.exit_code != 0
    assert "not a valid entry id" in r.stderr
    assert "Traceback" not in r.stdout + r.stderr


def test_supersede_with_a_malformed_id_exits_cleanly(env):
    r = runner.invoke(app, ["supersede", "abc", "--title", "T", "--body", "b"])
    assert r.exit_code != 0
    assert "not a valid entry id" in r.stderr


def test_commands_report_an_unreachable_postgres_without_a_traceback(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("REMEM_DSN", "postgresql://remem@127.0.0.1:1/remem")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))
    r = runner.invoke(app, ["search", "anything"])
    assert r.exit_code != 0
    assert "Cannot reach Postgres at postgresql://remem@127.0.0.1:1/remem" in r.stderr
    assert "docker compose up -d" in r.stderr
    assert "Traceback" not in r.stdout + r.stderr


@pytest.fixture
def unmigrated_dsn():
    """A freshly created database with no migrations applied."""
    import uuid

    import psycopg

    from tests.conftest import ADMIN_DSN, SKIP_REASON, _server_is_up

    if not _server_is_up():
        pytest.skip(SKIP_REASON)
    name = f"remem_bare_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'create database "{name}"')
    try:
        yield ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f'drop database if exists "{name}"')


def test_db_status_on_an_unmigrated_database_reports_pending(
    unmigrated_dsn, monkeypatch, tmp_path
):
    monkeypatch.setenv("REMEM_DSN", unmigrated_dsn)
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))
    r = runner.invoke(app, ["db", "status"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "Applied:   none" in r.stdout
    assert "001_initial" in r.stdout
    assert "Traceback" not in r.stdout + r.stderr


def test_db_migrate_on_an_unmigrated_database_applies_it(
    unmigrated_dsn, monkeypatch, tmp_path
):
    monkeypatch.setenv("REMEM_DSN", unmigrated_dsn)
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))
    r = runner.invoke(app, ["db", "migrate"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "001_initial" in r.stdout
    assert "Traceback" not in r.stdout + r.stderr

    again = runner.invoke(app, ["db", "status"])
    assert "Applied:   001_initial" in again.stdout


def test_install_with_project_scope_exits_nonzero(monkeypatch, tmp_path):
    """Never touches the real home: Path.home() is redirected, and the scope
    is rejected before any file is written anyway."""
    import pathlib

    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    r = runner.invoke(app, ["install", "claude-code", "--scope", "project"])
    assert r.exit_code != 0
    assert "user" in r.stderr
    assert "Traceback" not in r.stdout + r.stderr


def test_verify_reruns_the_install_round_trip_without_reinstalling(env):
    """`remem verify` is the re-runnable half of install's last step - a
    user who fixed the database, or just wants to check, should not have to
    reinstall to find out whether recording actually works."""
    r = runner.invoke(app, ["verify", "--agent", "claude-code"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "round-trip" in r.stdout
    assert r.stderr == ""


def test_verify_reports_an_unknown_agent(monkeypatch):
    r = runner.invoke(app, ["verify", "--agent", "no-such-agent"])
    assert r.exit_code != 0
    assert "no-such-agent" in r.stderr


def test_verify_exits_nonzero_when_it_cannot_prove_anything(monkeypatch):
    """Unlike install(), which folds a failed verification into a warning
    and finishes, `verify` is typed by a human asking "does this work?" -
    a report full of warnings must not still say yes."""
    monkeypatch.setenv("REMEM_DSN", "postgresql://nobody@127.0.0.1:1/none")
    r = runner.invoke(app, ["verify", "--agent", "claude-code"])
    assert r.exit_code != 0
    assert "warning" in r.stderr


def test_kb_new_without_a_query_warns(env):
    r = runner.invoke(app, ["kb", "new", "bare", "--title", "Bare"])
    assert r.exit_code == 0
    assert "--project" in r.stderr and "--tag" in r.stderr


def test_kb_new_with_a_project_does_not_warn(env):
    r = runner.invoke(app, ["kb", "new", "proj", "--title", "P",
                            "--project", "myapp"])
    assert r.exit_code == 0
    assert r.stderr.strip() == ""


def _summary_of(dsn, entry_id):
    """Read the stored summary directly.

    No CLI surface prints it - the summary exists to be exported into a
    Claude Code memory file's frontmatter - so asserting through the store
    tests what was written rather than what some formatter chose to show.
    """
    import psycopg
    with psycopg.connect(dsn) as c:
        row = c.execute(
            "select summary from entries where id = %s", (entry_id,)
        ).fetchone()
    return row[0]


def test_remember_stores_a_summary(env):
    r = runner.invoke(app, ["remember", "Has a summary", "--body", "b",
                            "--summary", "one line about it"])
    assert r.exit_code == 0, r.stdout
    assert _summary_of(env, r.stdout.strip()) == "one line about it"


def test_rule_stores_a_summary(env):
    r = runner.invoke(app, ["rule", "A rule with a summary", "--body", "b",
                            "--summary", "why the rule exists"])
    assert r.exit_code == 0, r.stdout
    assert _summary_of(env, r.stdout.strip()) == "why the rule exists"


def test_supersede_can_correct_a_summary(env):
    first = runner.invoke(app, ["remember", "Original", "--body", "b",
                                "--summary", "the old one"])
    assert first.exit_code == 0, first.stdout

    second = runner.invoke(app, ["supersede", first.stdout.strip(),
                                 "--title", "Corrected", "--body", "b2",
                                 "--summary", "the new one"])
    assert second.exit_code == 0, second.stdout
    assert _summary_of(env, second.stdout.strip()) == "the new one"


def test_supersede_without_a_summary_carries_the_old_one(env):
    # supersede's contract is that the replacement inherits everything the
    # caller did not restate. Adding the flag must not turn "omitted" into
    # "clear it" - that would silently empty the frontmatter description of
    # any memory file whose entry was ever corrected.
    first = runner.invoke(app, ["remember", "Original", "--body", "b",
                                "--summary", "kept across the correction"])
    assert first.exit_code == 0, first.stdout

    second = runner.invoke(app, ["supersede", first.stdout.strip(),
                                 "--title", "Corrected", "--body", "b2"])
    assert second.exit_code == 0, second.stdout
    assert _summary_of(env, second.stdout.strip()) == "kept across the correction"


def test_search_json_carries_the_summary(env):
    r = runner.invoke(app, ["remember", "Summarised", "--body", "long body here",
                            "--summary", "the one-line version"])
    assert r.exit_code == 0, r.stdout
    s = runner.invoke(app, ["search", "Summarised", "--json"])
    assert json.loads(s.stdout)[0]["summary"] == "the one-line version"


def test_get_json_carries_the_summary(env):
    runner.invoke(app, ["remember", "Gettable", "--body", "body",
                        "--summary", "read me back"])
    s = runner.invoke(app, ["search", "Gettable", "--json"])
    entry_id = json.loads(s.stdout)[0]["id"]
    g = runner.invoke(app, ["get", entry_id, "--json"])
    assert json.loads(g.stdout)["summary"] == "read me back"


def test_the_summary_key_is_present_and_null_when_there_is_none(env):
    # A key that appears only sometimes makes every consumer write a
    # membership test; nullable is the honest shape for a nullable column.
    runner.invoke(app, ["remember", "Unsummarised", "--body", "body"])
    s = runner.invoke(app, ["search", "Unsummarised", "--json"])
    assert json.loads(s.stdout)[0]["summary"] is None


def test_get_shows_the_summary_above_the_body(env):
    runner.invoke(app, ["remember", "Printable", "--body", "the body",
                        "--summary", "the gist"])
    s = runner.invoke(app, ["search", "Printable", "--json"])
    entry_id = json.loads(s.stdout)[0]["id"]
    g = runner.invoke(app, ["get", entry_id])
    assert "the gist" in g.stdout
    assert g.stdout.index("the gist") < g.stdout.index("the body")


def test_get_prints_no_summary_line_when_there_is_none(env):
    runner.invoke(app, ["remember", "Bare", "--body", "just a body"])
    s = runner.invoke(app, ["search", "Bare", "--json"])
    entry_id = json.loads(s.stdout)[0]["id"]
    g = runner.invoke(app, ["get", entry_id])
    assert g.stdout == "# Bare\n\njust a body\n"
