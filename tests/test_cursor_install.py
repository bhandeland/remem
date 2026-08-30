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
