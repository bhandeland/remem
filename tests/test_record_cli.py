"""`remem record event` - the CLI half of the write path.

This is a hook entry point in everything but name: it runs once per tool
call, must exit 0 unconditionally, and prints nothing on stdout unless
stdout is meant to be consumed. The one loud case is a human debugging by
hand, which is why REMEM_HOOK_DEBUG and --strict exist.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from remem.agents.base import HarnessEvent
from remem.agents.claude_code.adapter import ClaudeCodeAdapter
from remem.backends.postgres.migrate import migrate
from remem.cli import app
from tests.conftest import scalar

runner = CliRunner()

pytestmark = pytest.mark.db


@pytest.fixture
def env(live_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))
    return live_dsn


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "myrepo"
    root.mkdir()
    return root


def _payload(cwd: Path, **overrides: Any) -> dict[str, Any]:
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "s1",
        "cwd": str(cwd),
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }
    payload.update(overrides)
    return payload


def test_record_event_writes_one_row(env: str, repo: Path) -> None:
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


def test_record_event_records_nothing_when_the_project_has_not_opted_in(
    env: str, repo: Path
) -> None:
    result = runner.invoke(app, ["record", "event"], input=json.dumps(_payload(repo)))
    assert result.exit_code == 0
    with psycopg.connect(env) as c:
        count = scalar(c.execute("select count(*) from events"))
    assert count == 0


def test_record_event_exits_zero_on_garbage_stdin(env: str) -> None:
    """Fail-soft: this runs as a hook on every tool call."""
    result = runner.invoke(app, ["record", "event"], input="not json")
    assert result.exit_code == 0
    assert result.stdout == ""


def test_record_event_explains_itself_under_hook_debug(
    env: str, monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    monkeypatch.setenv("REMEM_HOOK_DEBUG", "1")
    result = runner.invoke(app, ["record", "event"], input=json.dumps(_payload(repo)))
    assert result.exit_code == 0
    assert "myrepo" in result.stderr
    assert "remem record enable" in result.stderr


def test_an_adapter_whose_event_capability_raises_degrades(
    env: str, monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    """Same contract as env_settings()/settings_path(): warn, degrade, keep
    going. A broken third-party adapter must never be why recording stops
    for everyone - and on this path, "stops" would be silent."""

    def boom(
        self: ClaudeCodeAdapter, env: Mapping[str, str], payload: dict[str, Any]
    ) -> HarnessEvent | None:
        raise RuntimeError("boom")

    monkeypatch.setattr(ClaudeCodeAdapter, "event", boom)
    result = runner.invoke(app, ["record", "event"], input=json.dumps(_payload(repo)))
    assert result.exit_code == 0


def test_an_adapter_with_no_event_capability_says_so(
    env: str, monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
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


def test_record_event_reads_a_named_agent(env: str, repo: Path) -> None:
    runner.invoke(app, ["record", "enable", "--project", "myrepo"])
    result = runner.invoke(
        app,
        ["record", "event", "--agent", "claude-code"],
        input=json.dumps(_payload(repo)),
    )
    assert result.exit_code == 0
    with psycopg.connect(env) as c:
        count = scalar(c.execute("select count(*) from events"))
    assert count == 1


def test_malformed_stdin_is_loud_under_strict(env: str) -> None:
    result = runner.invoke(app, ["record", "event", "--strict"], input="not json")
    assert result.exit_code == 1
    assert result.stderr != ""


def test_record_enable_then_disable(env: str) -> None:
    assert (
        runner.invoke(app, ["record", "enable", "--project", "myrepo"]).exit_code == 0
    )
    assert (
        runner.invoke(app, ["record", "disable", "--project", "myrepo"]).exit_code == 0
    )


def test_record_enable_states_the_model_and_cost(env: str) -> None:
    result = runner.invoke(app, ["record", "enable", "--project", "myrepo"])
    assert result.exit_code == 0
    out = result.stdout.lower()
    assert "sonnet" in out
    assert "REMEM_EXTRACT_MODEL".lower() in out
