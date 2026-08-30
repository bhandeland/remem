"""The seam between Cursor's payload and the adapter, which nothing
type-checks.

A rename on either side means a harness that records nothing, silently,
while every other test stays green. This file is what stands between a
rename and that outcome - the counterpart to tests/test_opencode_event.py.
"""

from __future__ import annotations

import subprocess

from remem.agents.cursor.adapter import ROOT_KEY, SESSION_KEY, CursorAdapter
from remem.domain import EventKind


def test_the_payload_keys_are_the_ones_cursor_sends():
    """Pinned as literals, not through the constants below - a test that
    builds its payload from SESSION_KEY cannot notice SESSION_KEY itself
    changing. This is the guard docs/superpowers/notes/2026-08-29-cursor-
    proof.md points at as standing between a rename here and a harness
    that silently records nothing; it has to spell the keys out, not
    reflect them back."""
    assert SESSION_KEY == "session_id"
    assert ROOT_KEY == "workspace_roots"


def _payload(hook: str, tmp_path, **extra) -> dict:
    # Literal key names, not SESSION_KEY/ROOT_KEY - see the test above.
    # Building this from the adapter's own constants would make every test
    # in this file pass even if both constants were renamed to garbage,
    # which is exactly the failure mode this file exists to catch.
    return {
        "hook_event_name": hook,
        "session_id": "sess-1",
        "workspace_roots": [str(tmp_path)],
        **extra,
    }


def test_a_tool_call_becomes_a_tool_call_event(tmp_path):
    adapter = CursorAdapter()

    event = adapter.event({}, _payload("postToolUse", tmp_path, tool_name="Shell"))

    assert event is not None
    assert event.kind is EventKind.TOOL_CALL
    assert event.session_id == "sess-1"
    assert event.tool == "Shell"


def test_both_message_hooks_become_message_events(tmp_path):
    adapter = CursorAdapter()

    for hook in ("beforeSubmitPrompt", "afterAgentResponse"):
        event = adapter.event({}, _payload(hook, tmp_path))
        assert event is not None, hook
        assert event.kind is EventKind.MESSAGE, hook


def test_session_start_is_not_an_event(tmp_path):
    """It injects rather than records, so it must never reach event() -
    exactly as opencode's system.transform never does."""
    adapter = CursorAdapter()

    assert adapter.event({}, _payload("sessionStart", tmp_path)) is None


def test_an_unsubscribed_hook_is_not_an_event(tmp_path):
    adapter = CursorAdapter()

    assert adapter.event({}, _payload("afterAgentThought", tmp_path)) is None
    assert adapter.event({}, _payload("beforeReadFile", tmp_path)) is None


def test_a_payload_with_no_session_id_is_not_an_event(tmp_path):
    """Without one the event cannot be grouped, and extraction is per
    session."""
    adapter = CursorAdapter()
    payload = _payload("postToolUse", tmp_path)
    del payload[SESSION_KEY]

    assert adapter.event({}, payload) is None


def test_a_payload_falls_back_to_conversation_id(tmp_path):
    """Cursor's own constructor computes session_id as
    (the hook-specific payload's own session_id) ?? conversation_id, so an
    adapter that only read SESSION_KEY would silently drop every event on a
    payload that still uses the older name."""
    adapter = CursorAdapter()
    payload = _payload("postToolUse", tmp_path)
    del payload[SESSION_KEY]
    payload["conversation_id"] = "sess-1"

    event = adapter.event({}, payload)

    assert event is not None
    assert event.session_id == "sess-1"


def test_the_payload_is_passed_through_whole(tmp_path):
    """The extractor is the half of this pipeline meant to be re-runnable
    without re-recording, so an adapter that pruned fields here would cap
    what any future extractor could ever see."""
    adapter = CursorAdapter()
    payload = _payload("postToolUse", tmp_path, weird_field={"nested": [1, 2]})

    event = adapter.event({}, payload)

    assert event is not None
    assert event.payload == payload


def test_identity_resolves_the_project_from_the_workspace_root(tmp_path):
    # A bare `mkdir()` for .git is not a repository: resolve_project shells
    # out to `git rev-parse --git-common-dir`, which needs a real one.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    adapter = CursorAdapter()

    identity = adapter.identity({}, _payload("postToolUse", tmp_path))

    assert identity.agent == "cursor"
    assert identity.session_id == "sess-1"
    assert identity.project == tmp_path.name


def test_identity_tolerates_a_bare_string_workspace_root(tmp_path):
    """Cursor's constructor always emits a list (a `.map()` over workspace
    folders), so this shape has never been observed live - but tolerating
    it costs nothing, and this pins that tolerance rather than the list
    assumption creeping back in unnoticed."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    adapter = CursorAdapter()
    payload = _payload("postToolUse", tmp_path)
    payload[ROOT_KEY] = str(tmp_path)

    identity = adapter.identity({}, payload)

    assert identity.project == tmp_path.name


def test_identity_of_an_empty_payload_is_not_an_error():
    """Every caller is a fail-soft hook."""
    adapter = CursorAdapter()

    identity = adapter.identity({}, {})

    assert identity.agent == "cursor"
    assert identity.session_id is None
    assert identity.project is None


def test_the_adapter_records_only_hooks_cursor_emits():
    from remem.agents.cursor.hooks import BLOCKING_HOOKS, HOOK_NAMES

    subscribed = frozenset(CursorAdapter.EVENT_KINDS)
    assert subscribed <= HOOK_NAMES
    assert not (subscribed & BLOCKING_HOOKS), (
        "a fail-soft hook must never sit where Cursor waits for a decision"
    )
