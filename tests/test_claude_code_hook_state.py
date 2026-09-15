"""What the installed settings.json actually registers.

The bug this exists for: ~/.claude/settings.json held three hooks, not
four - no PostToolUse - and Claude Code recorded zero tool calls for the
entire life of the events pipeline. Hooks are fail-soft, so nothing said
so.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from saddlebag.agents.claude_code.adapter import ClaudeCodeAdapter


def write_settings(home: Path, hooks: dict[str, Any]) -> Path:
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}))
    return path


def entry(command: str, timeout: int = 5):
    return {
        "matcher": "",
        "hooks": [{"type": "command", "command": command, "timeout": timeout}],
    }


def test_a_hook_that_is_not_registered_is_reported_as_absent(tmp_path: Path) -> None:
    write_settings(
        tmp_path,
        {
            "SessionStart": [entry("bag hook session-start")],
            "SessionEnd": [entry("bag hook record-event")],
            "UserPromptSubmit": [entry("bag hook session-size")],
        },
    )
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert state.found["PostToolUse"] == ()
    assert state.found["SessionStart"] == ("bag hook session-start",)


def test_a_hook_registered_twice_reports_both(tmp_path: Path) -> None:
    """525b491 infers this downstream from duplicate event rows. Read off
    the file it is visible before a single duplicate row is written."""
    write_settings(
        tmp_path,
        {
            "PostToolUse": [
                entry("bag hook record-event"),
                entry("bag hook record-event"),
            ],
        },
    )
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert state.found["PostToolUse"] == (
        "bag hook record-event",
        "bag hook record-event",
    )


def test_a_legacy_command_is_reported_as_the_command_it_actually_names(
    tmp_path: Path,
) -> None:
    """`bag hook session-end` is a real back-compat alias, so the hook
    still fires. Reporting it as MISSING would be a lie; the service calls
    it STALE."""
    write_settings(tmp_path, {"SessionEnd": [entry("bag hook session-end")]})
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert state.found["SessionEnd"] == ("bag hook session-end",)


def test_another_tool_s_hook_on_the_same_event_is_not_saddlebag_s_business(
    tmp_path: Path,
) -> None:
    write_settings(
        tmp_path,
        {
            "PostToolUse": [
                entry("some-other-tool --hook"),
                entry("bag hook record-event"),
            ],
        },
    )
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert state.found["PostToolUse"] == ("bag hook record-event",)


def test_no_settings_file_at_all_is_an_answer_not_an_error(tmp_path: Path) -> None:
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert state.exists is False
    assert all(v == () for v in state.found.values())


def test_unreadable_settings_are_not_rewritten(tmp_path: Path) -> None:
    """A check must never write. jsonfile.read_json backs a corrupt file up
    before returning {}, which is right for an install and wrong here."""
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json")
    before = sorted(p.name for p in path.parent.iterdir())
    ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert sorted(p.name for p in path.parent.iterdir()) == before


def test_the_expected_set_is_every_hook_the_adapter_installs(tmp_path: Path) -> None:
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert [h.event for h in state.expected] == [
        "SessionStart",
        "SessionEnd",
        "PostToolUse",
        "UserPromptSubmit",
    ]


def test_opencode_declines_the_capability_rather_than_answering_ok() -> None:
    """opencode ships a plugin file, not hook configuration, so there is no
    registration to check. It must DECLINE - an adapter that answered with
    an empty expected set would render as a clean bill of health for
    something never examined."""
    from saddlebag.agents.opencode.adapter import OpenCodeAdapter

    assert not hasattr(OpenCodeAdapter, "hook_state")


def test_a_missing_settings_file_still_names_the_path_examined(tmp_path: Path) -> None:
    """ "Not installed" without a path is indistinguishable from a check
    that looked somewhere else entirely - which is exactly how `saddlebag
    doctor` came to report cursor absent while it was installed."""
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert state.path == tmp_path / ".claude" / "settings.json"
    assert state.exists is False


def test_a_remem_era_command_is_found_not_reported_missing(tmp_path: Path) -> None:
    """After the rename, an install that has not been re-run still names
    `remem hook ...`. Reporting that as missing would send someone chasing a
    broken pipeline that is really a rename waiting for `bag install` - the
    same reason `bag hook session-end` is found rather than missing."""
    write_settings(tmp_path, {"SessionStart": [entry("remem hook session-start")]})
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert state.found["SessionStart"] == ("remem hook session-start",)
