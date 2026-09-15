import json
import os
from pathlib import Path
from typing import Any

import psycopg
import pytest

from saddlebag.agents.base import UnsupportedScope
from saddlebag.agents.claude_code.adapter import ClaudeCodeAdapter
from saddlebag.backends.postgres.migrate import migrate
from saddlebag.jsonfile import backup


def _store_env() -> dict[str, str]:
    """The scratch-database settings `_isolated_store` exported.

    For a test that hands install() an explicit env - to relocate
    CLAUDE_CONFIG_DIR - and so bypasses os.environ, where the fixture put
    them. Without this those tests round-tripped through the default
    address after the fixture had isolated everything else.
    """
    return {k: os.environ[k] for k in ("BAG_DSN", "BAG_USER_ID", "BAG_CONFIG")}


@pytest.fixture(autouse=True)
def _isolated_store(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Give every db test in this module a real scratch database.

    Nearly every test here calls install(), and install() folds verify()'s
    live round-trip into its report. None of them passed an env, so
    round_trip fell back to os.environ - no DSN on a developer machine - and
    resolved to the default address: the developer's own store. Thirty tests
    wrote and deleted a verify event there on every run and passed anyway,
    because a failed round-trip is a warning in the report and no test here
    asserts it succeeded.

    Autouse rather than a parameter on each test, because the defect was
    thirty tests each forgetting the same parameter; gated on the db marker
    so the pure tests beside them still run without Postgres.
    """
    if request.node.get_closest_marker("db") is None:
        return
    live_dsn: str = request.getfixturevalue("live_dsn")
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("BAG_DSN", live_dsn)
    monkeypatch.setenv("BAG_USER_ID", "brandon")
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))


@pytest.mark.db
def test_install_writes_the_mcp_server_entry(tmp_path: Path) -> None:
    report = ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    config = json.loads((tmp_path / ".claude.json").read_text())
    assert "saddlebag" in config["mcpServers"]
    assert config["mcpServers"]["saddlebag"]["command"] == "bag"
    assert config["mcpServers"]["saddlebag"]["args"] == ["serve"]
    assert any("mcp" in a.lower() for a in report.actions)


@pytest.mark.db
def test_install_preserves_existing_config(tmp_path: Path) -> None:
    existing = {"mcpServers": {"other": {"command": "other"}}, "theme": "dark"}
    (tmp_path / ".claude.json").write_text(json.dumps(existing))
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    config = json.loads((tmp_path / ".claude.json").read_text())
    assert "other" in config["mcpServers"]
    assert "saddlebag" in config["mcpServers"]
    assert config["theme"] == "dark"


@pytest.mark.db
def test_install_backs_up_before_overwriting(tmp_path: Path) -> None:
    (tmp_path / ".claude.json").write_text('{"theme": "dark"}')
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    backups = list(tmp_path.glob(".claude.json.bak*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == {"theme": "dark"}


@pytest.mark.db
def test_install_copies_the_skill(tmp_path: Path) -> None:
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    skill = tmp_path / ".claude" / "skills" / "bag" / "SKILL.md"
    assert skill.exists()
    assert "bag search" in skill.read_text()


@pytest.mark.db
def test_install_registers_the_session_start_hook(tmp_path: Path) -> None:
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    hooks = settings["hooks"]["SessionStart"]
    command = hooks[0]["hooks"][0]["command"]
    assert "bag hook session-start" in command


@pytest.mark.db
def test_install_is_idempotent(tmp_path: Path) -> None:
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["SessionStart"]) == 1
    # SessionEnd is what enqueues capture jobs; a duplicate block would
    # enqueue the same session twice on every exit.
    assert len(settings["hooks"]["SessionEnd"]) == 1
    config = json.loads((tmp_path / ".claude.json").read_text())
    assert list(config["mcpServers"]) == ["saddlebag"]
    # .claude.json is rewritten on every install (it already exists after the
    # first run), so the second run must back it up again.
    assert len(list(tmp_path.glob(".claude.json.bak*"))) == 1
    # settings.json is untouched on the second run (the hook is already
    # registered), so no second backup should be made.
    assert len(list((tmp_path / ".claude").glob("settings.json.bak*"))) == 0


@pytest.mark.db
def test_install_backs_up_hand_edits_made_between_installs(tmp_path: Path) -> None:
    (tmp_path / ".claude.json").write_text(json.dumps({"theme": "dark"}))
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)

    hand_edited = {"theme": "light", "custom": "value-added-by-hand"}
    (tmp_path / ".claude.json").write_text(json.dumps(hand_edited))
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)

    backups = list(tmp_path.glob(".claude.json.bak*"))
    assert len(backups) == 2
    contents = [json.loads(b.read_text()) for b in backups]
    assert hand_edited in contents


@pytest.mark.db
def test_install_backs_up_settings_before_overwriting(tmp_path: Path) -> None:
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


def test_backup_filenames_are_collision_safe(tmp_path: Path) -> None:
    path = tmp_path / ".claude.json"
    path.write_text("{}")
    first = backup(path)
    second = backup(path)
    assert first != second
    assert first.exists()
    assert second.exists()


@pytest.mark.db
def test_corrupt_existing_config_is_reported_not_silently_replaced(
    tmp_path: Path,
) -> None:
    (tmp_path / ".claude.json").write_text("{not valid json")
    report = ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    assert report.warnings
    assert list((tmp_path).glob(".claude.json.bak*"))


@pytest.mark.db
def test_the_install_registers_a_posttooluse_hook(tmp_path: Path) -> None:
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    command = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert "bag hook record-event" in command


@pytest.mark.db
def test_the_install_still_registers_session_start(tmp_path: Path) -> None:
    """Context injection is untouched by this change."""
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "bag hook session-start" in command


@pytest.mark.db
def test_the_posttooluse_hook_is_registered_once(tmp_path: Path) -> None:
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["PostToolUse"]) == 1


@pytest.mark.db
def test_install_registers_the_session_size_hook(tmp_path: Path) -> None:
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    command = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "bag hook session-size" in command


@pytest.mark.db
def test_the_session_size_hook_is_registered_once(tmp_path: Path) -> None:
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    # A duplicate block means two warnings on every prompt, which is how a
    # warning gets ignored.
    assert len(settings["hooks"]["UserPromptSubmit"]) == 1


@pytest.mark.db
def test_install_copies_every_bundled_skill(tmp_path: Path) -> None:
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    skills = tmp_path / ".claude" / "skills"
    assert (skills / "bag" / "SKILL.md").exists()
    assert (skills / "bag-handoff" / "SKILL.md").exists()
    assert (skills / "bag-prime" / "SKILL.md").exists()
    assert (skills / "bag-record" / "SKILL.md").exists()


@pytest.mark.db
def test_the_record_skill_leads_with_the_opt_in_gate(tmp_path: Path) -> None:
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    text = (tmp_path / ".claude" / "skills" / "bag-record" / "SKILL.md").read_text()
    # The gate is the whole safety story, and it is also the answer to the
    # question that brings anyone to this skill: nothing was recorded because
    # nobody turned it on.
    assert "bag record enable" in text


@pytest.mark.db
def test_the_record_skill_explains_the_context_block_exclusion(tmp_path: Path) -> None:
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    text = (tmp_path / ".claude" / "skills" / "bag-record" / "SKILL.md").read_text()
    # An agent that finds an extracted entry in `search` but never in a
    # context block will otherwise conclude extraction is broken. It is
    # deliberate, and `kb pin` is the way out.
    assert "bag kb pin" in text


@pytest.mark.db
def test_the_record_skill_points_at_the_failure_surface(tmp_path: Path) -> None:
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    text = (tmp_path / ".claude" / "skills" / "bag-record" / "SKILL.md").read_text()
    assert "bag record status" in text
    # Retrying a job past the attempt cap is the one recovery path that is not
    # discoverable from `--help` on the parent command.
    assert "--job" in text


@pytest.mark.db
def test_the_handoff_skill_drives_the_cli(tmp_path: Path) -> None:
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    text = (tmp_path / ".claude" / "skills" / "bag-handoff" / "SKILL.md").read_text()
    assert "bag handoff write" in text


@pytest.mark.db
def test_the_prime_skill_reads_the_latest_handoff(tmp_path: Path) -> None:
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    text = (tmp_path / ".claude" / "skills" / "bag-prime" / "SKILL.md").read_text()
    assert "bag handoff latest" in text


def test_identity_reads_the_hook_payload():
    ident = ClaudeCodeAdapter().identity(
        env={}, payload={"session_id": "abc", "cwd": "/x/y/saddlebag"}
    )
    assert ident.agent == "claude-code"
    assert ident.session_id == "abc"
    assert ident.project == "saddlebag"


def test_identity_tolerates_an_empty_payload():
    ident = ClaudeCodeAdapter().identity(env={}, payload={})
    assert ident.agent == "claude-code"
    assert ident.session_id is None
    assert ident.project is None


def test_install_rejects_project_scope(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedScope):
        ClaudeCodeAdapter().install(scope="project", home=tmp_path)
    assert not (tmp_path / ".claude.json").exists()


@pytest.mark.db
def test_install_states_the_hook_slug_convention(tmp_path: Path) -> None:
    report = ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    text = "\n".join(report.notes)
    assert "slug" in text and "repository name" in text
    assert "bag kb new" in text
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


@pytest.mark.db
def test_install_honours_claude_config_dir(tmp_path: Path) -> None:
    alt = tmp_path / "elsewhere"
    ClaudeCodeAdapter().install(
        scope="user", home=tmp_path, env={**_store_env(), "CLAUDE_CONFIG_DIR": str(alt)}
    )
    assert json.loads((alt / ".claude.json").read_text())["mcpServers"]["saddlebag"]
    assert (alt / "settings.json").exists()
    assert (alt / "skills" / "bag" / "SKILL.md").exists()


@pytest.mark.db
def test_install_writes_nothing_to_home_when_relocated(tmp_path: Path) -> None:
    alt = tmp_path / "elsewhere"
    ClaudeCodeAdapter().install(
        scope="user", home=tmp_path, env={**_store_env(), "CLAUDE_CONFIG_DIR": str(alt)}
    )
    assert not (tmp_path / ".claude.json").exists()
    assert not (tmp_path / ".claude").exists()


@pytest.mark.db
def test_install_falls_back_when_claude_config_dir_is_empty(tmp_path: Path) -> None:
    # An empty value is an unset value, not a request to write to the current
    # working directory, which is where Path("") would land.
    ClaudeCodeAdapter().install(
        scope="user", home=tmp_path, env={**_store_env(), "CLAUDE_CONFIG_DIR": ""}
    )
    assert (tmp_path / ".claude.json").exists()
    assert (tmp_path / ".claude" / "settings.json").exists()


@pytest.mark.db
def test_install_reports_the_relocated_directory(tmp_path: Path) -> None:
    alt = tmp_path / "elsewhere"
    report = ClaudeCodeAdapter().install(
        scope="user", home=tmp_path, env={**_store_env(), "CLAUDE_CONFIG_DIR": str(alt)}
    )
    # Without this the user sees a successful install with no hint that it
    # landed somewhere other than ~/.claude.
    assert any("CLAUDE_CONFIG_DIR" in n for n in report.notes)


@pytest.mark.db
def test_install_mentions_the_config_command(tmp_path: Path) -> None:
    # The install report is where someone learns what saddlebag can do for them
    # next; a command nobody is pointed at is a command nobody runs.
    report = ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    assert any("bag config" in n for n in report.notes)


@pytest.mark.db
def test_installing_over_a_pre_events_settings_file_does_not_double_register(
    tmp_path: Path,
) -> None:
    """The upgrade path, not the fresh install.

    `bag hook session-end` is a back-compat alias that runs exactly what
    `bag hook record-event` runs (cli.py) - it predates the idle trigger
    and stayed because an already-installed settings.json names it. So a
    settings file written before the events pipeline has SessionEnd pointing
    at the alias, and a membership test that only looks for the canonical
    command string sees "not registered", appends, and leaves BOTH. Two
    entries on one hook, both recording, and `events` has no unique
    constraint to catch it - every session close writes a duplicate row that
    the extractor then reads twice.

    Found on Brandon's own machine on 2026-08-30, by an install run to fix a
    missing PostToolUse hook.
    """
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "bag hook session-start",
                                    "timeout": 10,
                                }
                            ],
                        }
                    ],
                    "SessionEnd": [
                        {
                            "matcher": "",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "bag hook session-end",
                                    "timeout": 10,
                                }
                            ],
                        }
                    ],
                }
            }
        )
    )

    ClaudeCodeAdapter().install(scope="user", home=tmp_path)

    groups = json.loads(settings.read_text())["hooks"]["SessionEnd"]
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert commands == ["bag hook record-event"], (
        "the legacy alias should be migrated in place, not appended beside: "
        f"got {commands}"
    )


@pytest.mark.db
def test_migrating_the_legacy_session_end_alias_is_idempotent(tmp_path: Path) -> None:
    """A second install over the migrated file changes nothing further."""
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    groups = settings["hooks"]["SessionEnd"]
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert commands == ["bag hook record-event"]


@pytest.mark.db
def test_install_collapses_a_hook_already_registered_twice(tmp_path: Path) -> None:
    """Brandon's machine on 2026-08-30, after the bad install: SessionEnd
    named the legacy alias AND the canonical command, because a previous
    install had appended rather than migrated.

    Migrating in place is not enough here - rewriting the alias to the
    canonical command would leave two identical entries, which is the same
    duplicate-row bug wearing a different name. The repair has to collapse.
    """
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)

    def group(cmd: str, timeout: int) -> dict[str, Any]:
        return {
            "matcher": "",
            "hooks": [{"type": "command", "command": cmd, "timeout": timeout}],
        }

    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionEnd": [
                        group("bag hook session-end", 10),
                        group("bag hook record-event", 10),
                    ]
                }
            }
        )
    )

    ClaudeCodeAdapter().install(scope="user", home=tmp_path)

    groups = json.loads(settings.read_text())["hooks"]["SessionEnd"]
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert commands == ["bag hook record-event"], (
        f"the duplicate should be collapsed to one entry: got {commands}"
    )


@pytest.mark.db
def test_install_leaves_another_tools_hook_on_the_same_event_alone(
    tmp_path: Path,
) -> None:
    """The collapse is scoped to saddlebag's own commands.

    settings.json is shared - other tools register hooks on these same
    events, and an install that tidied the file by deleting entries it did
    not write would be far worse than the duplicate it set out to fix.
    """
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionEnd": [
                        {
                            "matcher": "",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "some-other-tool --flush",
                                    "timeout": 10,
                                }
                            ],
                        },
                        {
                            "matcher": "",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "bag hook session-end",
                                    "timeout": 10,
                                }
                            ],
                        },
                    ]
                }
            }
        )
    )

    ClaudeCodeAdapter().install(scope="user", home=tmp_path)

    groups = json.loads(settings.read_text())["hooks"]["SessionEnd"]
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert "some-other-tool --flush" in commands
    assert commands.count("bag hook record-event") == 1
