from __future__ import annotations

import json

from remem.agents.claude_code.env_vars import CLAUDE_CODE_ENV_VARS
from remem.services.settings import (
    Target,
    list_settings,
    shadow_warning,
    write_agent,
    write_remem,
)

TABLE = CLAUDE_CODE_ENV_VARS


def test_write_agent_creates_the_env_block(tmp_path):
    path = tmp_path / "settings.json"
    write_agent(path, "BASH_DEFAULT_TIMEOUT_MS", "600000")
    assert json.loads(path.read_text())["env"] == {
        "BASH_DEFAULT_TIMEOUT_MS": "600000"
    }


def test_write_agent_preserves_unrelated_settings(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {"SessionStart": []}, "env": {"X": "1"}}))
    write_agent(path, "BASH_DEFAULT_TIMEOUT_MS", "600000")
    data = json.loads(path.read_text())
    assert data["hooks"] == {"SessionStart": []}
    assert data["env"]["X"] == "1"
    assert data["env"]["BASH_DEFAULT_TIMEOUT_MS"] == "600000"


def test_write_agent_backs_up_before_touching_an_existing_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {}}))
    write_agent(path, "BASH_DEFAULT_TIMEOUT_MS", "600000")
    assert len(list(tmp_path.glob("settings.json.bak*"))) == 1


def test_write_agent_unsets_with_none(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"env": {"BASH_DEFAULT_TIMEOUT_MS": "1", "X": "2"}}))
    write_agent(path, "BASH_DEFAULT_TIMEOUT_MS", None)
    env = json.loads(path.read_text())["env"]
    assert "BASH_DEFAULT_TIMEOUT_MS" not in env
    assert env["X"] == "2"


def test_an_empty_string_is_written_not_removed(tmp_path):
    # Distinct from unset: "" is Claude Code's documented way to neutralise a
    # shell variable the user does not control.
    path = tmp_path / "settings.json"
    write_agent(path, "BASH_DEFAULT_TIMEOUT_MS", "")
    assert json.loads(path.read_text())["env"]["BASH_DEFAULT_TIMEOUT_MS"] == ""


def test_write_agent_survives_a_non_dict_env_block(tmp_path):
    # A hand-corrupted settings.json might have "env": "yes". This is a file
    # remem does not own, and every other touch of it degrades rather than
    # raising - setdefault("env", {}) would return the string and crash on
    # the next line, so write_agent must replace a non-dict env with a fresh
    # dict instead of propagating the corruption into a traceback.
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"env": "yes"}))
    write_agent(path, "BASH_DEFAULT_TIMEOUT_MS", "600000")
    data = json.loads(path.read_text())
    assert data["env"] == {"BASH_DEFAULT_TIMEOUT_MS": "600000"}


def test_a_remem_key_warns_when_an_export_shadows_the_file():
    # For remem, the environment beats config.toml, so writing the file while
    # the variable is exported is a silent no-op.
    warning = shadow_warning(Target.REMEM, "REMEM_MAX_CHARS", {"REMEM_MAX_CHARS": "1"})
    assert warning is not None
    assert "will not take effect" in warning


def test_a_remem_key_is_quiet_when_nothing_shadows_it():
    assert shadow_warning(Target.REMEM, "REMEM_MAX_CHARS", {}) is None


def test_an_agent_key_notes_that_it_overrides_the_export():
    # The opposite direction: settings.json beats a shell export.
    warning = shadow_warning(
        Target.AGENT, "BASH_DEFAULT_TIMEOUT_MS", {"BASH_DEFAULT_TIMEOUT_MS": "1"}
    )
    assert warning is not None
    assert "overrides" in warning


def test_an_agent_key_is_quiet_when_nothing_is_exported():
    assert shadow_warning(Target.AGENT, "BASH_DEFAULT_TIMEOUT_MS", {}) is None


def test_list_reports_the_default_when_nothing_is_set(tmp_path):
    settings = list_settings(
        tmp_path / "config.toml", tmp_path / "settings.json", TABLE, {}
    )
    row = next(s for s in settings if s.key == "BASH_DEFAULT_TIMEOUT_MS")
    assert row.value == "120000"
    assert row.source == "default"


def test_list_reports_the_file_as_the_source(tmp_path):
    remem_path = tmp_path / "config.toml"
    write_remem(remem_path, "REMEM_MAX_CHARS", "8000")
    settings = list_settings(remem_path, tmp_path / "settings.json", TABLE, {})
    row = next(s for s in settings if s.key == "REMEM_MAX_CHARS")
    assert row.value == "8000"
    assert row.source == "file"


def test_list_reports_the_environment_winning_for_a_remem_key(tmp_path):
    remem_path = tmp_path / "config.toml"
    write_remem(remem_path, "REMEM_MAX_CHARS", "8000")
    settings = list_settings(
        remem_path, tmp_path / "settings.json", TABLE, {"REMEM_MAX_CHARS": "999"}
    )
    row = next(s for s in settings if s.key == "REMEM_MAX_CHARS")
    assert row.value == "999"
    assert row.source == "environment"


def test_list_reports_the_file_winning_for_an_agent_key(tmp_path):
    # The asymmetry: for Claude Code the file beats the export.
    agent_path = tmp_path / "settings.json"
    write_agent(agent_path, "BASH_DEFAULT_TIMEOUT_MS", "600000")
    settings = list_settings(
        tmp_path / "config.toml",
        agent_path,
        TABLE,
        {"BASH_DEFAULT_TIMEOUT_MS": "999"},
    )
    row = next(s for s in settings if s.key == "BASH_DEFAULT_TIMEOUT_MS")
    assert row.value == "600000"
    assert row.source == "file"
