"""Creating project memories on demand.

The friction these close: `--project` was required, so forgetting it stored an
entry with no project - a silent orphan the project's knowledge base would
never show, even though the write succeeded.
"""

import json

import psycopg
import pytest
from typer.testing import CliRunner

from remem.backends.postgres.migrate import migrate
from remem.cli import app

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


def _entry(query):
    payload = json.loads(
        runner.invoke(app, ["search", query, "--json"]).stdout
    )
    return payload[0] if payload else None


# --- project defaulting -----------------------------------------------------


@pytest.mark.db
def test_remember_defaults_project_to_the_directory(env, monkeypatch, tmp_path):
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    runner.invoke(app, ["remember", "Defaulted", "--body", "shared body"])
    assert _entry("shared")["project"] == "myproj"


@pytest.mark.db
def test_an_explicit_project_still_wins(env, monkeypatch, tmp_path):
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    runner.invoke(app, ["remember", "Explicit", "--body", "shared body",
                        "--project", "other"])
    assert _entry("shared")["project"] == "other"


@pytest.mark.db
def test_global_writes_an_entry_with_no_project(env, monkeypatch, tmp_path):
    """Cross-project knowledge has to stay expressible now that project
    defaults - otherwise every memory silently belongs to a directory."""
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    runner.invoke(app, ["remember", "Everywhere", "--body", "shared body",
                        "--global"])
    assert _entry("shared")["project"] is None


@pytest.mark.db
def test_global_and_project_together_are_refused(env):
    result = runner.invoke(app, ["remember", "T", "--body", "b",
                                 "--global", "--project", "x"])
    assert result.exit_code != 0


# --- the rule shorthand -----------------------------------------------------


@pytest.mark.db
def test_rule_writes_a_rule(env, monkeypatch, tmp_path):
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    result = runner.invoke(app, ["rule", "Spaced hyphens",
                                 "--body", "never em dashes",
                                 "--summary", "Use spaced hyphens, never em dashes"])
    assert result.exit_code == 0, result.stdout
    entry = _entry("hyphens")
    assert entry["kind"] == "rule"
    assert entry["project"] == "myproj"


@pytest.mark.db
def test_rule_accepts_tags_and_global(env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["rule", "Universal", "--body", "applies everywhere",
                        "--summary", "This rule applies everywhere",
                        "--tag", "style", "--global"])
    entry = _entry("everywhere")
    assert entry["project"] is None
    assert entry["tags"] == ["style"]


# --- editor-composed bodies -------------------------------------------------


def test_editor_body_uses_the_configured_editor(monkeypatch):
    from remem import cli

    seen = {}

    def fake_call(cmd, **kwargs):
        seen["cmd"] = cmd
        with open(cmd[-1], "w") as fh:
            fh.write("written in the editor\n")
        return 0

    monkeypatch.setattr(cli.subprocess, "call", fake_call)
    monkeypatch.setenv("EDITOR", "my-editor")
    assert cli._body_from_editor() == "written in the editor"
    assert seen["cmd"][0] == "my-editor"


def test_editor_falls_back_when_no_editor_is_set(monkeypatch):
    from remem import cli

    seen = {}

    def fake_call(cmd, **kwargs):
        seen["cmd"] = cmd
        with open(cmd[-1], "w") as fh:
            fh.write("body\n")
        return 0

    monkeypatch.setattr(cli.subprocess, "call", fake_call)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    cli._body_from_editor()
    assert seen["cmd"][0] == "vi"


def test_an_empty_editor_body_aborts(monkeypatch):
    """Storing a blank entry because the user closed the editor without
    writing is worse than doing nothing."""
    import typer

    from remem import cli

    def fake_call(cmd, **kwargs):
        with open(cmd[-1], "w") as fh:
            fh.write("   \n\n")
        return 0

    monkeypatch.setattr(cli.subprocess, "call", fake_call)
    with pytest.raises(typer.Exit):
        cli._body_from_editor()


@pytest.mark.db
def test_remember_edit_stores_the_edited_body(env, monkeypatch, tmp_path):
    from remem import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli, "_body_from_editor", lambda initial="": "composed in the editor"
    )
    result = runner.invoke(app, ["remember", "Edited", "--edit"])
    assert result.exit_code == 0, result.stdout
    assert "composed" in _entry("composed")["snippet"]


# --- the agent side ---------------------------------------------------------


@pytest.mark.db
def test_mcp_remember_defaults_project_to_the_directory(env, monkeypatch, tmp_path):
    """An agent that omits project would otherwise write an orphan it can
    never find again through the project's knowledge base."""
    project_dir = tmp_path / "agentproj"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    from remem.mcp_server import remember_tool

    remember_tool(title="Agent wrote this", body="distinctive agent body")
    assert _entry("distinctive")["project"] == "agentproj"


@pytest.mark.db
def test_mcp_remember_honours_an_explicit_project(env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from remem.mcp_server import remember_tool

    remember_tool(title="Explicit", body="distinctive explicit body",
                  project="chosen")
    assert _entry("distinctive")["project"] == "chosen"
