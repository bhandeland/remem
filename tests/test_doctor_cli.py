"""`remem doctor`. Reads files, opens no database."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from remem.cli import app

runner = CliRunner()


def settings_with(tmp_path, hooks):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}))
    return path


def entry(command):
    return {"matcher": "", "hooks": [{"type": "command", "command": command}]}


COMPLETE = {
    "SessionStart": [entry("remem hook session-start")],
    "SessionEnd": [entry("remem hook record-event")],
    "PostToolUse": [entry("remem hook record-event")],
    "UserPromptSubmit": [entry("remem hook session-size")],
}


def test_a_missing_required_hook_is_named_and_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    hooks = dict(COMPLETE)
    del hooks["PostToolUse"]
    settings_with(tmp_path, hooks)
    result = runner.invoke(app, ["doctor", "claude-code"])
    assert result.exit_code == 1
    assert "PostToolUse" in result.stdout
    assert "remem install claude-code" in result.stdout


def test_a_complete_install_exits_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    settings_with(tmp_path, COMPLETE)
    result = runner.invoke(app, ["doctor", "claude-code"])
    assert result.exit_code == 0
    assert "MISSING" not in result.stdout


def test_json_carries_the_same_verdict_and_exit_code(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    hooks = dict(COMPLETE)
    del hooks["PostToolUse"]
    settings_with(tmp_path, hooks)
    result = runner.invoke(app, ["doctor", "claude-code", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["failed"] is True


def test_an_unknown_agent_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(app, ["doctor", "no-such-harness"])
    assert result.exit_code == 1
    assert "no-such-harness" in result.stdout


def test_the_scope_flag_reaches_the_adapter(tmp_path, monkeypatch):
    """Claude Code implements only user scope and raises UnsupportedScope
    for anything else. That raise must land where the registry contract
    says it lands - unchecked with a warning, not a traceback."""
    monkeypatch.setenv("HOME", str(tmp_path))
    settings_with(tmp_path, COMPLETE)
    result = runner.invoke(app, ["doctor", "claude-code", "--scope", "project"])
    assert result.exit_code == 0
    assert "project" in result.stdout


def test_doctor_needs_no_database(tmp_path, monkeypatch):
    """The whole point of a diagnostic: it has to work when the system is
    unhealthy. services/settings.py is the existing precedent."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("REMEM_DSN", "postgresql://nobody@127.0.0.1:1/nothing")
    settings_with(tmp_path, COMPLETE)
    result = runner.invoke(app, ["doctor", "claude-code"])
    assert result.exit_code == 0


def test_not_installed_names_the_file_it_looked_in(tmp_path, monkeypatch):
    """The bar this feature is held to: never a confident answer about
    something that was not examined. "not installed" is a confident answer,
    so it has to say where it looked."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["doctor", "claude-code"])
    assert result.exit_code == 0
    assert "not installed" in result.stdout
    assert str(tmp_path / ".claude" / "settings.json") in result.stdout


def test_one_file_is_reported_looked_in_once(tmp_path, monkeypatch):
    """cwd == home resolves cursor's user and project scope to the same
    hooks.json. Printing "looked in" twice for one file reads as two places
    checked, which overstates the search behind a "not installed" - the one
    thing this line exists to keep honest. cursor, not claude-code: claude
    code raises UnsupportedScope for project scope, so a sweep only ever
    examines one file for it and could never duplicate."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["doctor", "cursor"])
    assert result.exit_code == 0
    assert "not installed" in result.stdout
    assert result.stdout.count("looked in") == 1
