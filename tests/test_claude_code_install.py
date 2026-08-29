import json

import pytest

from remem.agents.base import UnsupportedScope
from remem.agents.claude_code.adapter import ClaudeCodeAdapter
from remem.jsonfile import backup


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
    path = tmp_path / ".claude.json"
    path.write_text("{}")
    first = backup(path)
    second = backup(path)
    assert first != second
    assert first.exists()
    assert second.exists()


def test_corrupt_existing_config_is_reported_not_silently_replaced(tmp_path):
    (tmp_path / ".claude.json").write_text("{not valid json")
    report = ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    assert report.warnings
    assert list((tmp_path).glob(".claude.json.bak*"))


def test_the_install_registers_a_posttooluse_hook(tmp_path):
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    command = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert "remem hook record-event" in command


def test_the_install_still_registers_session_start(tmp_path):
    """Context injection is untouched by this change."""
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "remem hook session-start" in command


def test_the_posttooluse_hook_is_registered_once(tmp_path):
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["PostToolUse"]) == 1


def test_install_registers_the_session_size_hook(tmp_path):
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    command = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "remem hook session-size" in command


def test_the_session_size_hook_is_registered_once(tmp_path):
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    # A duplicate block means two warnings on every prompt, which is how a
    # warning gets ignored.
    assert len(settings["hooks"]["UserPromptSubmit"]) == 1


def test_install_copies_every_bundled_skill(tmp_path):
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    skills = tmp_path / ".claude" / "skills"
    assert (skills / "remem" / "SKILL.md").exists()
    assert (skills / "remem-handoff" / "SKILL.md").exists()
    assert (skills / "remem-prime" / "SKILL.md").exists()
    assert (skills / "remem-capture" / "SKILL.md").exists()


def test_the_capture_skill_leads_with_the_opt_in_gate(tmp_path):
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    text = (tmp_path / ".claude" / "skills" / "remem-capture" / "SKILL.md").read_text()
    # The gate is the whole safety story, and it is also the answer to the
    # question that brings anyone to this skill: nothing was captured because
    # nobody turned it on.
    assert "remem capture enable" in text


def test_the_capture_skill_explains_the_context_block_exclusion(tmp_path):
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    text = (tmp_path / ".claude" / "skills" / "remem-capture" / "SKILL.md").read_text()
    # An agent that finds a captured entry in `search` but never in a context
    # block will otherwise conclude capture is broken. It is deliberate, and
    # `kb pin` is the way out.
    assert "remem kb pin" in text


def test_the_capture_skill_points_at_the_failure_surface(tmp_path):
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    text = (tmp_path / ".claude" / "skills" / "remem-capture" / "SKILL.md").read_text()
    assert "remem capture status" in text
    # Retrying a job past the attempt cap is the one recovery path that is not
    # discoverable from `--help` on the parent command.
    assert "--job" in text


def test_the_handoff_skill_drives_the_cli(tmp_path):
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    text = (tmp_path / ".claude" / "skills" / "remem-handoff" / "SKILL.md").read_text()
    assert "remem handoff write" in text


def test_the_prime_skill_reads_the_latest_handoff(tmp_path):
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    text = (tmp_path / ".claude" / "skills" / "remem-prime" / "SKILL.md").read_text()
    assert "remem handoff latest" in text


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
    assert "slug" in text and "repository name" in text
    assert "remem kb new" in text
    # The convention people get wrong: a subdirectory or worktree is the same
    # project, so the note has to say so rather than just naming the rule.
    assert "worktree" in text or "subdirectory" in text


# CLAUDE_CONFIG_DIR relocation. Verified against the shipped Claude Code binary
# (2.1.247), which resolves both targets from the same variable:
#
#   .claude.json  ->  join(process.env.CLAUDE_CONFIG_DIR || homedir(), ".claude.json")
#   the directory ->  process.env.CLAUDE_CONFIG_DIR || join(homedir(), ".claude")
#
# Note the asymmetry: when the variable is set, .claude.json moves *inside* the
# config directory rather than staying beside it. Getting this wrong fails
# silently - the hooks are fail-soft and the MCP server simply never starts.


def test_install_honours_claude_config_dir(tmp_path):
    alt = tmp_path / "elsewhere"
    ClaudeCodeAdapter().install(
        scope="user", home=tmp_path, env={"CLAUDE_CONFIG_DIR": str(alt)}
    )
    assert json.loads((alt / ".claude.json").read_text())["mcpServers"]["remem"]
    assert (alt / "settings.json").exists()
    assert (alt / "skills" / "remem" / "SKILL.md").exists()


def test_install_writes_nothing_to_home_when_relocated(tmp_path):
    alt = tmp_path / "elsewhere"
    ClaudeCodeAdapter().install(
        scope="user", home=tmp_path, env={"CLAUDE_CONFIG_DIR": str(alt)}
    )
    assert not (tmp_path / ".claude.json").exists()
    assert not (tmp_path / ".claude").exists()


def test_install_falls_back_when_claude_config_dir_is_empty(tmp_path):
    # An empty value is an unset value, not a request to write to the current
    # working directory, which is where Path("") would land.
    ClaudeCodeAdapter().install(
        scope="user", home=tmp_path, env={"CLAUDE_CONFIG_DIR": ""}
    )
    assert (tmp_path / ".claude.json").exists()
    assert (tmp_path / ".claude" / "settings.json").exists()


def test_install_reports_the_relocated_directory(tmp_path):
    alt = tmp_path / "elsewhere"
    report = ClaudeCodeAdapter().install(
        scope="user", home=tmp_path, env={"CLAUDE_CONFIG_DIR": str(alt)}
    )
    # Without this the user sees a successful install with no hint that it
    # landed somewhere other than ~/.claude.
    assert any("CLAUDE_CONFIG_DIR" in n for n in report.notes)


def test_install_mentions_the_config_command(tmp_path):
    # The install report is where someone learns what remem can do for them
    # next; a command nobody is pointed at is a command nobody runs.
    report = ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    assert any("remem config" in n for n in report.notes)
