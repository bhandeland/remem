"""`remem record event` - the CLI half of the write path.

This is a hook entry point in everything but name: it runs once per tool
call, must exit 0 unconditionally, and prints nothing on stdout unless
stdout is meant to be consumed. The one loud case is a human debugging by
hand, which is why REMEM_HOOK_DEBUG and --strict exist.
"""

from __future__ import annotations

import json

import psycopg
import pytest
from typer.testing import CliRunner

from remem.agents.claude_code.adapter import ClaudeCodeAdapter
from remem.backends.postgres.migrate import migrate
from remem.cli import app

runner = CliRunner()

pytestmark = pytest.mark.db


@pytest.fixture
def env(live_dsn, monkeypatch, tmp_path):
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))
    return live_dsn


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "myrepo"
    root.mkdir()
    return root


def _payload(cwd, **overrides):
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "s1",
        "cwd": str(cwd),
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }
    payload.update(overrides)
    return payload


def test_record_event_writes_one_row(env, repo):
    runner.invoke(app, ["record", "enable", "--project", "myrepo"])

    result = runner.invoke(app, ["record", "event"], input=json.dumps(_payload(repo)))

    assert result.exit_code == 0
    with psycopg.connect(env) as c:
        rows = c.execute("select kind, tool, payload from events").fetchall()
    assert len(rows) == 1
    kind, tool, payload = rows[0]
    assert kind == "tool_call"
    assert tool == "Bash"
    assert payload["tool_input"] == {"command": "ls"}


def test_record_event_records_nothing_when_the_project_has_not_opted_in(env, repo):
    result = runner.invoke(app, ["record", "event"], input=json.dumps(_payload(repo)))
    assert result.exit_code == 0
    with psycopg.connect(env) as c:
        count = c.execute("select count(*) from events").fetchone()[0]
    assert count == 0


def test_record_event_exits_zero_on_garbage_stdin(env):
    """Fail-soft: this runs as a hook on every tool call."""
    result = runner.invoke(app, ["record", "event"], input="not json")
    assert result.exit_code == 0
    assert result.stdout == ""


def test_record_event_explains_itself_under_hook_debug(env, monkeypatch, repo):
    monkeypatch.setenv("REMEM_HOOK_DEBUG", "1")
    result = runner.invoke(app, ["record", "event"], input=json.dumps(_payload(repo)))
    assert result.exit_code == 0
    assert "myrepo" in result.stderr
    assert "remem record enable" in result.stderr


def test_an_adapter_whose_event_capability_raises_degrades(env, monkeypatch, repo):
    """Same contract as env_settings()/settings_path(): warn, degrade, keep
    going. A broken third-party adapter must never be why recording stops
    for everyone - and on this path, "stops" would be silent."""

    def boom(self, env, payload):
        raise RuntimeError("boom")

    monkeypatch.setattr(ClaudeCodeAdapter, "event", boom)
    result = runner.invoke(app, ["record", "event"], input=json.dumps(_payload(repo)))
    assert result.exit_code == 0


def test_an_adapter_with_no_event_capability_says_so(env, monkeypatch, repo):
    """Not an error and not a crash - "this agent cannot record" is an
    answer, and it is the one Cursor and opencode give until their adapters
    ship."""
    monkeypatch.delattr(ClaudeCodeAdapter, "event")
    monkeypatch.setenv("REMEM_HOOK_DEBUG", "1")

    result = runner.invoke(app, ["record", "event"], input=json.dumps(_payload(repo)))

    assert result.exit_code == 0
    assert (
        "does not support" in result.stderr.lower()
        or "no event" in result.stderr.lower()
    )


def test_record_event_reads_a_named_agent(env, repo):
    runner.invoke(app, ["record", "enable", "--project", "myrepo"])
    result = runner.invoke(
        app,
        ["record", "event", "--agent", "claude-code"],
        input=json.dumps(_payload(repo)),
    )
    assert result.exit_code == 0
    with psycopg.connect(env) as c:
        count = c.execute("select count(*) from events").fetchone()[0]
    assert count == 1


def test_malformed_stdin_is_loud_under_strict(env):
    result = runner.invoke(app, ["record", "event", "--strict"], input="not json")
    assert result.exit_code == 1
    assert result.stderr != ""


def test_record_enable_then_disable(env):
    assert (
        runner.invoke(app, ["record", "enable", "--project", "myrepo"]).exit_code == 0
    )
    assert (
        runner.invoke(app, ["record", "disable", "--project", "myrepo"]).exit_code == 0
    )


def test_record_enable_states_the_model_and_cost(env):
    result = runner.invoke(app, ["record", "enable", "--project", "myrepo"])
    assert result.exit_code == 0
    out = result.stdout.lower()
    assert "sonnet" in out
    assert "REMEM_EXTRACT_MODEL".lower() in out
