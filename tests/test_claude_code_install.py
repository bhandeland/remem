import json

import pytest

from remem.agents.base import UnsupportedScope
from remem.agents.claude_code.adapter import ClaudeCodeAdapter


def test_install_writes_the_mcp_server_entry(tmp_path):
    report = ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    config = json.loads((tmp_path / ".claude.json").read_text())
    assert "remem" in config["mcpServers"]
    assert config["mcpServers"]["remem"]["command"] == "remem"
    assert config["mcpServers"]["remem"]["args"] == ["serve"]
    assert any("mcp" in a.lower() for a in report.actions)


def test_install_preserves_existing_config(tmp_path):
    existing = {"mcpServers": {"other": {"command": "other"}}, "theme": "dark"}
    (tmp_path / ".claude.json").write_text(json.dumps(existing))
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    config = json.loads((tmp_path / ".claude.json").read_text())
    assert "other" in config["mcpServers"]
    assert "remem" in config["mcpServers"]
    assert config["theme"] == "dark"


def test_install_backs_up_before_overwriting(tmp_path):
    (tmp_path / ".claude.json").write_text('{"theme": "dark"}')
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    backups = list(tmp_path.glob(".claude.json.bak*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == {"theme": "dark"}


def test_install_copies_the_skill(tmp_path):
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    skill = tmp_path / ".claude" / "skills" / "remem" / "SKILL.md"
    assert skill.exists()
    assert "remem search" in skill.read_text()


def test_install_registers_the_session_start_hook(tmp_path):
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    hooks = settings["hooks"]["SessionStart"]
    command = hooks[0]["hooks"][0]["command"]
    assert "remem hook session-start" in command


def test_install_is_idempotent(tmp_path):
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["SessionStart"]) == 1
    # SessionEnd is what enqueues capture jobs; a duplicate block would
    # enqueue the same session twice on every exit.
    assert len(settings["hooks"]["SessionEnd"]) == 1
    config = json.loads((tmp_path / ".claude.json").read_text())
    assert list(config["mcpServers"]) == ["remem"]
    # .claude.json is rewritten on every install (it already exists after the
    # first run), so the second run must back it up again.
    assert len(list(tmp_path.glob(".claude.json.bak*"))) == 1
    # settings.json is untouched on the second run (the hook is already
    # registered), so no second backup should be made.
    assert len(list((tmp_path / ".claude").glob("settings.json.bak*"))) == 0


def test_install_backs_up_hand_edits_made_between_installs(tmp_path):
    (tmp_path / ".claude.json").write_text(json.dumps({"theme": "dark"}))
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)

    hand_edited = {"theme": "light", "custom": "value-added-by-hand"}
    (tmp_path / ".claude.json").write_text(json.dumps(hand_edited))
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)

    backups = list(tmp_path.glob(".claude.json.bak*"))
    assert len(backups) == 2
    contents = [json.loads(b.read_text()) for b in backups]
    assert hand_edited in contents


def test_install_backs_up_settings_before_overwriting(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    existing = {"model": "opus", "permissions": {"allow": ["Bash"]}}
    (claude_dir / "settings.json").write_text(json.dumps(existing))

    ClaudeCodeAdapter().install(scope="user", home=tmp_path)

    backups = list(claude_dir.glob("settings.json.bak*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == existing

    settings = json.loads((claude_dir / "settings.json").read_text())
    assert settings["model"] == "opus"
    assert settings["permissions"] == {"allow": ["Bash"]}


def test_backup_filenames_are_collision_safe(tmp_path):
    from remem.agents.claude_code.adapter import _backup

    path = tmp_path / ".claude.json"
    path.write_text("{}")
    first = _backup(path)
    second = _backup(path)
    assert first != second
    assert first.exists()
    assert second.exists()


def test_corrupt_existing_config_is_reported_not_silently_replaced(tmp_path):
    (tmp_path / ".claude.json").write_text("{not valid json")
    report = ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    assert report.warnings
    assert list((tmp_path).glob(".claude.json.bak*"))


def test_identity_reads_the_hook_payload():
    ident = ClaudeCodeAdapter().identity(
        env={}, payload={"session_id": "abc", "cwd": "/x/y/remem"}
    )
    assert ident.agent == "claude-code"
    assert ident.session_id == "abc"
    assert ident.project == "remem"


def test_identity_tolerates_an_empty_payload():
    ident = ClaudeCodeAdapter().identity(env={}, payload={})
    assert ident.agent == "claude-code"
    assert ident.session_id is None
    assert ident.project is None


def test_install_rejects_project_scope(tmp_path):
    with pytest.raises(UnsupportedScope):
        ClaudeCodeAdapter().install(scope="project", home=tmp_path)
    assert not (tmp_path / ".claude.json").exists()


def test_install_states_the_hook_slug_convention(tmp_path):
    report = ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    text = "\n".join(report.notes)
    assert "slug" in text and "directory name" in text
    assert "remem kb new" in text
