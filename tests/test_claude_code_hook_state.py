"""What the installed settings.json actually registers.

The bug this exists for: ~/.claude/settings.json held three hooks, not
four - no PostToolUse - and Claude Code recorded zero tool calls for the
entire life of the events pipeline. Hooks are fail-soft, so nothing said
so.
"""

from __future__ import annotations

import json

from remem.agents.claude_code.adapter import ClaudeCodeAdapter


def write_settings(home, hooks):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}))
    return path


def entry(command, timeout=5):
    return {"matcher": "", "hooks": [{"type": "command", "command": command,
                                      "timeout": timeout}]}


def test_a_hook_that_is_not_registered_is_reported_as_absent(tmp_path):
    write_settings(tmp_path, {
        "SessionStart": [entry("remem hook session-start")],
        "SessionEnd": [entry("remem hook record-event")],
        "UserPromptSubmit": [entry("remem hook session-size")],
    })
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert state.found["PostToolUse"] == ()
    assert state.found["SessionStart"] == ("remem hook session-start",)


def test_a_hook_registered_twice_reports_both(tmp_path):
    """525b491 infers this downstream from duplicate event rows. Read off
    the file it is visible before a single duplicate row is written."""
    write_settings(tmp_path, {
        "PostToolUse": [entry("remem hook record-event"),
                        entry("remem hook record-event")],
    })
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert state.found["PostToolUse"] == (
        "remem hook record-event", "remem hook record-event",
    )


def test_a_legacy_command_is_reported_as_the_command_it_actually_names(tmp_path):
    """`remem hook session-end` is a real back-compat alias, so the hook
    still fires. Reporting it as MISSING would be a lie; the service calls
    it STALE."""
    write_settings(tmp_path, {"SessionEnd": [entry("remem hook session-end")]})
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert state.found["SessionEnd"] == ("remem hook session-end",)


def test_another_tool_s_hook_on_the_same_event_is_not_remem_s_business(tmp_path):
    write_settings(tmp_path, {
        "PostToolUse": [entry("some-other-tool --hook"),
                        entry("remem hook record-event")],
    })
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert state.found["PostToolUse"] == ("remem hook record-event",)


def test_no_settings_file_at_all_is_an_answer_not_an_error(tmp_path):
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert state.path is None
    assert all(v == () for v in state.found.values())


def test_unreadable_settings_are_not_rewritten(tmp_path):
    """A check must never write. jsonfile.read_json backs a corrupt file up
    before returning {}, which is right for an install and wrong here."""
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json")
    before = sorted(p.name for p in path.parent.iterdir())
    ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert sorted(p.name for p in path.parent.iterdir()) == before


def test_the_expected_set_is_every_hook_the_adapter_installs(tmp_path):
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert [h.event for h in state.expected] == [
        "SessionStart", "SessionEnd", "PostToolUse", "UserPromptSubmit",
    ]


def test_opencode_declines_the_capability_rather_than_answering_ok():
    """opencode ships a plugin file, not hook configuration, so there is no
    registration to check. It must DECLINE - an adapter that answered with
    an empty expected set would render as a clean bill of health for
    something never examined."""
    from remem.agents.opencode.adapter import OpenCodeAdapter

    assert not hasattr(OpenCodeAdapter, "hook_state")
