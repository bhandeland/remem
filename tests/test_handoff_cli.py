import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.cli import app

pytestmark = pytest.mark.db

runner = CliRunner()

BODY = "## Done\nlanded it\n\n## In flight\n\n## Next steps\n\n## Gotchas\n"


@pytest.fixture
def env(live_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    import psycopg

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("BAG_DSN", live_dsn)
    monkeypatch.setenv("BAG_USER_ID", "brandon")
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))
    return live_dsn


def test_write_then_latest_round_trips(env: str) -> None:
    w = runner.invoke(
        app,
        ["handoff", "write", "--topic", "ci", "--project", "saddlebag", "--body", BODY],
    )
    assert w.exit_code == 0, w.stdout

    r = runner.invoke(
        app, ["handoff", "latest", "--topic", "ci", "--project", "saddlebag", "--json"]
    )
    assert r.exit_code == 0
    payload = json.loads(r.stdout)
    assert payload["title"].startswith("Handoff: ci")
    assert "landed it" in payload["body"]


def test_write_reports_what_it_superseded(env: str) -> None:
    runner.invoke(
        app,
        ["handoff", "write", "--topic", "ci", "--project", "saddlebag", "--body", BODY],
    )
    second = runner.invoke(
        app,
        ["handoff", "write", "--topic", "ci", "--project", "saddlebag", "--body", BODY],
    )
    assert "superseded" in second.stdout


def test_write_reads_the_body_from_stdin(env: str) -> None:
    r = runner.invoke(
        app,
        ["handoff", "write", "--topic", "ci", "--project", "saddlebag", "--body", "-"],
        input=BODY,
    )
    assert r.exit_code == 0, r.stdout


def test_latest_with_nothing_stored_is_not_an_error(env: str) -> None:
    r = runner.invoke(app, ["handoff", "latest", "--project", "saddlebag"])
    assert r.exit_code == 0
    assert "No handoff" in r.stdout


def test_a_handoff_with_no_project_fails_loudly(
    env: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fail-loud, unlike the hooks: the user is standing there about to throw
    the session's context away."""
    monkeypatch.setattr("saddlebag.cli._default_project", lambda: None)
    r = runner.invoke(app, ["handoff", "write", "--body", BODY])
    assert r.exit_code == 1
    assert "project" in r.stderr


def test_an_unslugable_topic_fails_loudly_and_does_not_touch_another_topic(
    env: str,
) -> None:
    """The CLI-level version of the silent-data-loss regression test: writing
    an unslug-able topic must not supersede an unrelated live handoff."""
    first = runner.invoke(
        app,
        ["handoff", "write", "--topic", "ci", "--project", "saddlebag", "--body", BODY],
    )
    assert first.exit_code == 0, first.stdout

    bad = runner.invoke(
        app,
        [
            "handoff",
            "write",
            "--topic",
            "!!!",
            "--project",
            "saddlebag",
            "--body",
            BODY,
        ],
    )
    assert bad.exit_code == 1
    assert "topic" in bad.stderr

    r = runner.invoke(
        app, ["handoff", "latest", "--topic", "ci", "--project", "saddlebag", "--json"]
    )
    payload = json.loads(r.stdout)
    assert "landed it" in payload["body"]


def test_latest_with_an_unslugable_topic_fails_loudly(env: str) -> None:
    runner.invoke(
        app,
        ["handoff", "write", "--topic", "ci", "--project", "saddlebag", "--body", BODY],
    )
    r = runner.invoke(
        app, ["handoff", "latest", "--topic", "!!!", "--project", "saddlebag"]
    )
    assert r.exit_code == 1
    assert "topic" in r.stderr
