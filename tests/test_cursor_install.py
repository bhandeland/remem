from __future__ import annotations

import json

import pytest

from remem.agents.base import UnsupportedScope
from remem.agents.cursor import install


def test_user_scope_is_the_home_cursor_directory(tmp_path):
    path = install.hooks_path("user", home=tmp_path, cwd=tmp_path / "repo")

    assert path == tmp_path / ".cursor" / "hooks.json"


def test_project_scope_is_the_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    path = install.hooks_path("project", home=tmp_path, cwd=repo)

    assert path == repo / ".cursor" / "hooks.json"


def test_an_unknown_scope_raises_rather_than_falling_back(tmp_path):
    with pytest.raises(UnsupportedScope):
        install.hooks_path("global", home=tmp_path, cwd=tmp_path)


def test_merging_into_nothing_creates_the_document(tmp_path):
    path = tmp_path / "hooks.json"

    merged, backup = install.merge(path, {"sessionStart": "remem hook context"})

    assert backup is None
    assert merged["version"] == 1
    assert merged["hooks"]["sessionStart"] == [{"command": "remem hook context"}]


def test_merging_preserves_another_tools_hooks(tmp_path):
    """hooks.json is user-owned and shared - unlike opencode's remem.js,
    which remem is the only thing that ever writes."""
    path = tmp_path / "hooks.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "stop": [{"command": "someone-elses-tool"}],
                    "sessionStart": [{"command": "also-theirs"}],
                },
            }
        )
    )

    merged, backup = install.merge(path, {"sessionStart": "remem hook context"})

    assert merged["hooks"]["stop"] == [{"command": "someone-elses-tool"}]
    commands = [h["command"] for h in merged["hooks"]["sessionStart"]]
    assert "also-theirs" in commands
    assert "remem hook context" in commands
    assert backup is not None and backup.exists()


def test_merging_twice_does_not_duplicate_our_entry(tmp_path):
    path = tmp_path / "hooks.json"
    entries = {"sessionStart": "remem hook context"}

    merged, _ = install.merge(path, entries)
    path.write_text(json.dumps(merged))
    merged, _ = install.merge(path, entries)

    assert len(merged["hooks"]["sessionStart"]) == 1


def test_the_backup_holds_what_was_there_before(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text('{"version": 1, "hooks": {"stop": [{"command": "x"}]}}')

    _, backup = install.merge(path, {"sessionStart": "remem hook context"})

    assert backup is not None
    assert json.loads(backup.read_text())["hooks"]["stop"] == [{"command": "x"}]


def test_unreadable_json_is_backed_up_and_replaced(tmp_path):
    """A corrupt hooks.json must not stop the install, but the user's bytes
    must survive - which is exactly what the backup is for."""
    path = tmp_path / "hooks.json"
    path.write_text("{not json at all")

    merged, backup = install.merge(path, {"sessionStart": "remem hook context"})

    assert merged["hooks"]["sessionStart"] == [{"command": "remem hook context"}]
    assert backup is not None
    assert backup.read_text() == "{not json at all"


def test_the_installed_entries_name_only_hooks_cursor_emits():
    from remem.agents.cursor.hooks import BLOCKING_HOOKS, HOOK_NAMES

    named = frozenset(install.ENTRIES)

    assert named <= HOOK_NAMES, (
        f"install would subscribe to hooks Cursor does not emit: "
        f"{sorted(named - HOOK_NAMES)}"
    )
    assert not (named & BLOCKING_HOOKS)


def test_every_recorded_hook_is_also_installed():
    """ENTRIES and EVENT_KINDS are each checked against HOOK_NAMES and
    BLOCKING_HOOKS separately, but never against each other - so adding a
    hook to one table and forgetting the other installs (or drops) a hook
    that fires and records nothing, silently, with every other test green.

    The relationship is a subset, not equality: ENTRIES also has
    `sessionStart`, which injects the knowledge base rather than recording
    an event and is deliberately absent from EVENT_KINDS (see
    CursorAdapter.EVENT_KINDS's docstring). So the invariant this test
    makes executable is "every hook the adapter parses is also installed",
    not "the two tables name the same hooks".
    """
    from remem.agents.cursor.adapter import CursorAdapter

    assert frozenset(CursorAdapter.EVENT_KINDS) <= frozenset(install.ENTRIES)


def test_the_entries_call_the_harness_neutral_command():
    """`remem hook record-event` is hardcoded to Claude Code and takes no
    --agent. Getting this wrong records nothing, silently."""
    for hook, command in install.ENTRIES.items():
        assert "--agent cursor" in command
        assert "hook record-event" not in command


@pytest.mark.db
def test_install_writes_the_hooks_and_verifies(tmp_path, monkeypatch):
    """install() performs a live database round-trip - it proves the
    record path actually works - which is why this is marked db."""
    from remem.agents.cursor.adapter import CursorAdapter

    monkeypatch.chdir(tmp_path)
    report = CursorAdapter().install(scope="user", home=tmp_path, env=None)

    written = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())
    assert set(written["hooks"]) == set(install.ENTRIES)
    assert any("round-trip" in a for a in report.actions), report.warnings
    assert any("Recording is OFF" in n for n in report.notes)


def test_merge_collapses_a_command_the_file_already_names_twice(tmp_path):
    """The repair half, mirroring the Claude Code adapter's.

    The membership test below only ever prevented a duplicate this install
    would add; it never fixed one already in the file. A hooks.json that
    names remem's command twice - hand-edited, or written by a buggy
    earlier install - fires the hook twice and doubles every row it
    records, and `events` has no unique constraint to catch it.
    """
    path = tmp_path / "hooks.json"
    command = "remem record event --agent cursor"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {"postToolUse": [{"command": command}, {"command": command}]},
            }
        )
    )

    merged, _ = install.merge(path, {"postToolUse": command})

    assert merged["hooks"]["postToolUse"] == [{"command": command}]


def test_merge_migrates_a_superseded_command_instead_of_appending_beside_it(tmp_path):
    """The migration half.

    Renaming a command remem writes would otherwise leave the old entry in
    place next to the new one - both firing - because the membership test
    is by exact string. This is exactly what happened to the Claude Code
    adapter's SessionEnd hook; the table is empty here only because no
    cursor command has been renamed yet, so the mechanism is exercised
    with a supplied one.
    """
    path = tmp_path / "hooks.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {"postToolUse": [{"command": "remem record event --old"}]},
            }
        )
    )

    merged, _ = install.merge(
        path,
        {"postToolUse": "remem record event --agent cursor"},
        legacy={"postToolUse": ("remem record event --old",)},
    )

    assert merged["hooks"]["postToolUse"] == [
        {"command": "remem record event --agent cursor"}
    ]


def test_merge_repairs_a_file_naming_both_the_old_and_the_new_command(tmp_path):
    """The state a previous buggy install leaves behind: both present.

    Migrating in place is not enough here - it would produce two identical
    entries, the same duplicate bug wearing a different name.
    """
    path = tmp_path / "hooks.json"
    new = "remem record event --agent cursor"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "postToolUse": [
                        {"command": "remem record event --old"},
                        {"command": new},
                    ]
                },
            }
        )
    )

    merged, _ = install.merge(
        path,
        {"postToolUse": new},
        legacy={"postToolUse": ("remem record event --old",)},
    )

    assert merged["hooks"]["postToolUse"] == [{"command": new}]


def test_merge_never_removes_an_entry_remem_did_not_write(tmp_path):
    """hooks.json is shared and user-owned.

    An install that tidied the file by deleting entries it did not write
    would be far worse than the duplicate it set out to fix - including a
    duplicate that belongs to somebody else.
    """
    path = tmp_path / "hooks.json"
    command = "remem record event --agent cursor"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "postToolUse": [
                        {"command": "other-tool --log"},
                        {"command": "other-tool --log"},
                        {"command": command},
                        {"command": command},
                    ]
                },
            }
        )
    )

    merged, _ = install.merge(path, {"postToolUse": command})

    assert merged["hooks"]["postToolUse"] == [
        {"command": "other-tool --log"},
        {"command": "other-tool --log"},
        {"command": command},
    ]


def test_the_hook_table_names_exactly_what_install_writes():
    from remem.agents.cursor.install import HOOK_ENTRIES

    assert sorted(h.event for h in HOOK_ENTRIES) == sorted(
        [
            "sessionStart",
            "postToolUse",
            "beforeSubmitPrompt",
            "afterAgentResponse",
        ]
    )


def test_merge_preserves_other_keys_on_an_entry_it_rewrites(tmp_path):
    """Cursor may grow per-entry options. Rewriting the command must not
    drop whatever else the entry carried."""
    path = tmp_path / "hooks.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "postToolUse": [
                        {"command": "remem record event --old", "timeout": 7}
                    ]
                },
            }
        )
    )

    merged, _ = install.merge(
        path,
        {"postToolUse": "remem record event --agent cursor"},
        legacy={"postToolUse": ("remem record event --old",)},
    )

    assert merged["hooks"]["postToolUse"] == [
        {"command": "remem record event --agent cursor", "timeout": 7}
    ]
