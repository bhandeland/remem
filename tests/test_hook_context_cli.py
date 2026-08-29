"""`remem hook context` - the harness-neutral half of SessionStart.

Same contract as `remem record event` (tests/test_record_cli.py): a hook
entry point in everything but name, so it must exit 0 unconditionally and
print nothing but the block itself to stdout. Every early return explains
itself only through REMEM_HOOK_DEBUG on stderr.
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


def test_context_exits_zero_on_garbage_stdin(env):
    """Fail-soft: malformed stdin must not be why the block is missing."""
    result = runner.invoke(app, ["hook", "context"], input="not json")
    assert result.exit_code == 0
    assert result.stdout == ""


def test_context_explains_garbage_stdin_under_hook_debug(env, monkeypatch):
    monkeypatch.setenv("REMEM_HOOK_DEBUG", "1")
    result = runner.invoke(app, ["hook", "context"], input="not json")
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "not valid JSON" in result.stderr


def test_context_exits_zero_for_an_unknown_agent(env, repo):
    result = runner.invoke(
        app,
        ["hook", "context", "--agent", "no-such-agent"],
        input=json.dumps({"cwd": str(repo), "session_id": "x"}),
    )
    assert result.exit_code == 0
    assert result.stdout == ""


def test_context_explains_an_unknown_agent_under_hook_debug(env, monkeypatch, repo):
    monkeypatch.setenv("REMEM_HOOK_DEBUG", "1")
    result = runner.invoke(
        app,
        ["hook", "context", "--agent", "no-such-agent"],
        input=json.dumps({"cwd": str(repo), "session_id": "x"}),
    )
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "no-such-agent" in result.stderr


def test_context_exits_zero_when_the_payload_carries_no_cwd(env):
    """No cwd means identity.project is None - an ordinary "cannot place
    this session" answer, not an error."""
    result = runner.invoke(
        app, ["hook", "context"], input=json.dumps({"session_id": "x"})
    )
    assert result.exit_code == 0
    assert result.stdout == ""


def test_context_explains_a_missing_cwd_under_hook_debug(env, monkeypatch):
    monkeypatch.setenv("REMEM_HOOK_DEBUG", "1")
    result = runner.invoke(
        app, ["hook", "context"], input=json.dumps({"session_id": "x"})
    )
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "no project" in result.stderr.lower() or "cwd" in result.stderr.lower()


def test_an_adapter_whose_identity_capability_raises_degrades(env, monkeypatch, repo):
    """Same contract as event()/env_settings()/settings_path(): a broken
    third-party adapter must never be why the block is missing for
    everyone - it just costs the block, silently."""

    def boom(self, env, payload):
        raise RuntimeError("boom")

    monkeypatch.setattr(ClaudeCodeAdapter, "identity", boom)
    result = runner.invoke(
        app,
        ["hook", "context"],
        input=json.dumps({"cwd": str(repo), "session_id": "x"}),
    )
    assert result.exit_code == 0
    assert result.stdout == ""


def test_an_adapter_whose_identity_capability_raises_explains_itself(
    env, monkeypatch, repo
):
    monkeypatch.setenv("REMEM_HOOK_DEBUG", "1")

    def boom(self, env, payload):
        raise RuntimeError("boom")

    monkeypatch.setattr(ClaudeCodeAdapter, "identity", boom)
    result = runner.invoke(
        app,
        ["hook", "context"],
        input=json.dumps({"cwd": str(repo), "session_id": "x"}),
    )
    assert result.exit_code == 0
    assert "identity" in result.stderr
    assert "boom" in result.stderr


def test_context_reads_a_named_agent(env, repo):
    """The opencode adapter reads sessionID, not session_id - same
    difference record event's --agent absorbs, exercised here on the
    injection half instead of the recording half."""
    result = runner.invoke(
        app,
        ["hook", "context", "--agent", "opencode"],
        input=json.dumps({"cwd": str(repo), "sessionID": "x"}),
    )
    assert result.exit_code == 0
    assert result.stdout == ""
