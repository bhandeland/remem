"""The last of the capture -> events rename: names outside the database.

Everything here is about the seam between an old world and a new one - a
model variable a shell profile still exports, a child-process guard that must
move on every side at once or not at all, and commands people still type from
memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.cli import app
from saddlebag.config import load

runner = CliRunner()


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


def test_the_old_model_variable_still_works_and_warns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One release of grace, and a warning that names the replacement.

    A user's shell profile or settings.json holds BAG_CAPTURE_MODEL today.
    Reading it silently would leave them believing they had pinned a model
    they had not; ignoring it silently would switch their model without
    telling them.
    """
    config = load(env={"BAG_CAPTURE_MODEL": "opus"})
    assert config.extract_model == "opus"
    assert "BAG_EXTRACT_MODEL" in capsys.readouterr().err


def test_the_new_variable_wins_when_both_are_set(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = load(env={"BAG_CAPTURE_MODEL": "haiku", "BAG_EXTRACT_MODEL": "opus"})
    assert config.extract_model == "opus"


def test_the_child_variable_is_named_consistently_everywhere() -> None:
    """One string, checked by three hooks and set by the extractor. A
    rename that reaches only some of them lets an extraction's own child
    record events, which the next extraction reads, without bound."""
    from saddlebag.extract.base import CHILD_ENV_VAR

    assert CHILD_ENV_VAR == "BAG_EXTRACT_CHILD"
    source = (Path("src") / "saddlebag").rglob("*.py")
    for path in source:
        text = path.read_text()
        assert "BAG_CAPTURE_CHILD" not in text, path


@pytest.mark.db
def test_the_capture_commands_still_run_and_warn(env: str) -> None:
    """The warning is on stderr, not stdout - an old crontab still calling
    `capture drain` every hour should not gain a permanent line of stdout
    noise, and `capture status --json` has to stay parseable on its own."""
    result = runner.invoke(app, ["capture", "enable", "--project", "x"])
    assert result.exit_code == 0
    assert "bag record enable" in result.stderr


def test_no_credential_or_endpoint_variable_became_settable() -> None:
    """Standing rule, re-asserted because this task edits the table."""
    from saddlebag.services.settings import BAG_VARS

    for forbidden in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_VERTEX_BASE_URL",
        "CLAUDE_CONFIG_DIR",
        "BAG_CONFIG",
    ):
        assert forbidden not in BAG_VARS
