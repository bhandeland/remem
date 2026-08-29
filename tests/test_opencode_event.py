"""Reading opencode plugin payloads as events.

The payload shape here is the contract with plugin.js: change one and the
other stops recording, silently, because `remem record event` is fail-soft
by design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remem.agents.opencode.adapter import OpenCodeAdapter
from remem.domain import EventKind


@pytest.fixture
def repo(tmp_path):
    """A real git repository, so resolve_project has something to resolve."""
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def test_tool_call_becomes_a_tool_call_event(repo):
    payload = {
        "hook": "tool.execute.after",
        "sessionID": "ses_abc123",
        "cwd": str(repo),
        "tool": "bash",
        "callID": "call_1",
        "args": {"command": "ls"},
        "result": {"title": "ls", "output": "README.md", "metadata": {}},
    }

    event = OpenCodeAdapter().event({}, payload)

    assert event is not None
    assert event.kind is EventKind.TOOL_CALL
    assert event.session_id == "ses_abc123"
    assert event.tool == "bash"
    assert event.project == Path(repo).name


def test_chat_message_becomes_a_message_event(repo):
    payload = {
        "hook": "chat.message",
        "sessionID": "ses_abc123",
        "cwd": str(repo),
        "message": {"id": "msg_1", "role": "user"},
        "parts": [{"type": "text", "text": "hello"}],
    }

    event = OpenCodeAdapter().event({}, payload)

    assert event is not None
    assert event.kind is EventKind.MESSAGE
    assert event.tool is None


def test_the_whole_payload_is_carried_through_unparsed(repo):
    """The extractor is the half of the pipeline meant to be re-runnable.

    An adapter that picked fields out here would decide, at record time and
    forever, what a future extractor is allowed to see.
    """
    payload = {
        "hook": "tool.execute.after",
        "sessionID": "ses_abc123",
        "cwd": str(repo),
        "tool": "bash",
        "surprise": {"nested": [1, 2, 3]},
    }

    event = OpenCodeAdapter().event({}, payload)

    assert event.payload["surprise"] == {"nested": [1, 2, 3]}


def test_an_unsubscribed_hook_records_nothing(repo):
    payload = {"hook": "chat.params", "sessionID": "ses_abc123", "cwd": str(repo)}

    assert OpenCodeAdapter().event({}, payload) is None


def test_a_payload_with_no_session_id_records_nothing(repo):
    """Without a session id an event cannot be grouped for extraction, so
    recording it would be storage with no reader."""
    payload = {"hook": "tool.execute.after", "cwd": str(repo), "tool": "bash"}

    assert OpenCodeAdapter().event({}, payload) is None


def test_the_adapter_is_discoverable_under_its_entry_point():
    """Registry discovery reads *installed* entry points, so this test fails
    until the package is reinstalled - see the task's note on `uv sync`."""
    from remem.agents import registry

    assert registry.get("opencode") is OpenCodeAdapter


def test_the_adapter_declares_no_env_capabilities():
    """env_settings() and settings_path() are a pair, and remem has no
    curated table of opencode variables. An adapter with one of them cannot
    be written to but looks as though it can."""
    adapter = OpenCodeAdapter()

    assert not hasattr(adapter, "env_settings")
    assert not hasattr(adapter, "settings_path")
