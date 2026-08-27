import json

import pytest
from typer.testing import CliRunner

from remem.backends.postgres.migrate import migrate
from remem.cli import app

pytestmark = pytest.mark.db

runner = CliRunner()

BODY = "## Done\nlanded it\n\n## In flight\n\n## Next steps\n\n## Gotchas\n"


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


def test_write_then_latest_round_trips(env):
    w = runner.invoke(app, ["handoff", "write", "--topic", "ci",
                            "--project", "remem", "--body", BODY])
    assert w.exit_code == 0, w.stdout

    r = runner.invoke(app, ["handoff", "latest", "--topic", "ci",
                            "--project", "remem", "--json"])
    assert r.exit_code == 0
    payload = json.loads(r.stdout)
    assert payload["title"].startswith("Handoff: ci")
    assert "landed it" in payload["body"]


def test_write_reports_what_it_superseded(env):
    runner.invoke(app, ["handoff", "write", "--topic", "ci",
                        "--project", "remem", "--body", BODY])
    second = runner.invoke(app, ["handoff", "write", "--topic", "ci",
                                 "--project", "remem", "--body", BODY])
    assert "superseded" in second.stdout


def test_write_reads_the_body_from_stdin(env):
    r = runner.invoke(app, ["handoff", "write", "--topic", "ci",
                            "--project", "remem", "--body", "-"], input=BODY)
    assert r.exit_code == 0, r.stdout


def test_latest_with_nothing_stored_is_not_an_error(env):
    r = runner.invoke(app, ["handoff", "latest", "--project", "remem"])
    assert r.exit_code == 0
    assert "No handoff" in r.stdout


def test_a_handoff_with_no_project_fails_loudly(env, monkeypatch, tmp_path):
    """Fail-loud, unlike the hooks: the user is standing there about to throw
    the session's context away."""
    monkeypatch.setattr("remem.cli._default_project", lambda: None)
    r = runner.invoke(app, ["handoff", "write", "--body", BODY])
    assert r.exit_code == 1
    assert "project" in r.stderr
