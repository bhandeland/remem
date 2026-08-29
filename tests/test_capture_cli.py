import json

import psycopg
import pytest
from typer.testing import CliRunner

from remem.agents.claude_code.adapter import ClaudeCodeAdapter
from remem.backends.postgres.migrate import migrate
from remem.cli import app

runner = CliRunner()


def test_installer_registers_the_session_end_hook(tmp_path):
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    commands = [
        h["command"]
        for group in settings["hooks"]["SessionEnd"]
        for h in group["hooks"]
    ]
    assert any("remem hook session-end" in c for c in commands)


def test_installing_twice_leaves_one_session_end_hook(tmp_path):
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["SessionEnd"]) == 1


def test_install_report_says_capture_is_off_by_default(tmp_path):
    report = ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    combined = " ".join(report.actions + report.notes).lower()
    assert "capture" in combined
    assert "enable" in combined


@pytest.fixture
def env(live_dsn, monkeypatch, tmp_path):
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))
    return live_dsn


@pytest.mark.db
def test_enable_then_status_reports_the_project(env):
    assert runner.invoke(app, ["capture", "enable", "--project", "remem"]).exit_code == 0
    result = runner.invoke(app, ["capture", "status"])
    assert result.exit_code == 0
    assert "remem" in result.stdout


@pytest.mark.db
def test_status_json_is_valid_when_nothing_has_happened(env):
    result = runner.invoke(app, ["capture", "status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["counts"] == {}
    assert payload["failures"] == []
    assert payload["enabled_projects"] == []


@pytest.mark.db
def test_disable_removes_the_project_from_status(env):
    runner.invoke(app, ["capture", "enable", "--project", "remem"])
    runner.invoke(app, ["capture", "disable", "--project", "remem"])
    payload = json.loads(
        runner.invoke(app, ["capture", "status", "--json"]).stdout
    )
    assert payload["enabled_projects"] == []


@pytest.mark.db
def test_enable_states_the_model_and_cost(env):
    """Borrowed from claude-mem, which quotes a rate at install time. The
    moment a user opts in is the moment the tradeoff is actionable."""
    result = runner.invoke(app, ["capture", "enable", "--project", "remem"])
    assert result.exit_code == 0
    out = result.stdout.lower()
    assert "sonnet" in out
    assert "REMEM_CAPTURE_MODEL".lower() in out


@pytest.mark.db
def test_status_reports_the_configured_model(env):
    payload = json.loads(
        runner.invoke(app, ["capture", "status", "--json"]).stdout
    )
    assert payload["model"] == "sonnet"
