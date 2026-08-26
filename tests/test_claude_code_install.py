import json

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
    config = json.loads((tmp_path / ".claude.json").read_text())
    assert list(config["mcpServers"]) == ["remem"]


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
