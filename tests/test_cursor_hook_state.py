"""What .cursor/hooks.json actually registers."""

from __future__ import annotations

import json

from remem.agents.cursor.adapter import CursorAdapter

RECORD = "remem record event --agent cursor"
CONTEXT = "remem hook context --agent cursor"


def write_hooks(home, hooks):
    path = home / ".cursor" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "hooks": hooks}))
    return path


def test_a_missing_message_hook_is_reported(tmp_path):
    write_hooks(
        tmp_path,
        {
            "sessionStart": [{"command": CONTEXT}],
            "postToolUse": [{"command": RECORD}],
            "beforeSubmitPrompt": [{"command": RECORD}],
        },
    )
    state = CursorAdapter().hook_state("user", tmp_path, {})
    assert state.found["afterAgentResponse"] == ()


def test_another_tool_s_cursor_hook_is_left_out_of_the_report(tmp_path):
    write_hooks(
        tmp_path,
        {
            "postToolUse": [{"command": "other-tool"}, {"command": RECORD}],
        },
    )
    state = CursorAdapter().hook_state("user", tmp_path, {})
    assert state.found["postToolUse"] == (RECORD,)


def test_both_message_hooks_are_required(tmp_path):
    """Marked optional in the first draft, on the grounds that losing them
    costs extraction quality rather than recording. 8cb186c measured that
    distinction away: a fragment of a session returned nothing in three
    runs where the whole session returned entries in five of five."""
    state = CursorAdapter().hook_state("user", tmp_path, {})
    required = {h.event for h in state.expected if h.required}
    assert required == {
        "sessionStart",
        "postToolUse",
        "beforeSubmitPrompt",
        "afterAgentResponse",
    }


def test_no_hooks_file_is_an_answer_not_an_error(tmp_path):
    state = CursorAdapter().hook_state("user", tmp_path, {})
    assert state.exists is False
    assert all(v == () for v in state.found.values())


def test_project_scope_reads_the_repository_s_own_hooks_file(tmp_path, monkeypatch):
    """Cursor is the one adapter with two real scopes, and hooks_path
    resolves project scope from cwd - so the check has to look where the
    install wrote, not where the user's home is."""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".cursor" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"version": 1, "hooks": {"postToolUse": [{"command": RECORD}]}})
    )
    state = CursorAdapter().hook_state("project", tmp_path / "elsewhere", {})
    assert state.found["postToolUse"] == (RECORD,)
    assert state.path == path


def test_a_missing_hooks_file_still_names_the_path_examined(tmp_path):
    state = CursorAdapter().hook_state("user", tmp_path, {})
    assert state.path == tmp_path / ".cursor" / "hooks.json"
    assert state.exists is False
