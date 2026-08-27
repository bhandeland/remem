from __future__ import annotations

import pytest

from remem.agents.base import Kind
from remem.agents.claude_code.adapter import ClaudeCodeAdapter
from remem.agents.claude_code.env_vars import CLAUDE_CODE_ENV_VARS


def test_the_adapter_exposes_its_env_table():
    table = ClaudeCodeAdapter().env_settings()
    assert table["BASH_DEFAULT_TIMEOUT_MS"].kind is Kind.INT
    assert table["BASH_DEFAULT_TIMEOUT_MS"].default == "120000"


def test_every_entry_carries_one_line_of_help():
    # The help string is what makes `remem config list` self-documenting,
    # which is the entire return on choosing a curated allowlist.
    for var in CLAUDE_CODE_ENV_VARS.values():
        assert var.help.strip()
        assert "\n" not in var.help


def test_the_table_keys_match_the_variable_names():
    for key, var in CLAUDE_CODE_ENV_VARS.items():
        assert key == var.name


@pytest.mark.parametrize(
    "forbidden",
    [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_VERTEX_BASE_URL",
    ],
)
def test_no_credential_or_endpoint_is_ever_settable(forbidden):
    # remem must never be the tool that writes a credential into a JSON file
    # on disk, and repointing an agent at another inference endpoint silently
    # exfiltrates prompts. Excluded by design, asserted so it stays that way.
    assert forbidden not in CLAUDE_CODE_ENV_VARS


def test_claude_config_dir_is_not_settable():
    # It names the file that would store it. Setting it inside settings.json
    # is a chicken-and-egg that reads as broken, and the Claude Code docs say
    # to set it in the shell.
    assert "CLAUDE_CONFIG_DIR" not in CLAUDE_CODE_ENV_VARS


def test_the_presence_only_toggles_are_marked_as_such():
    # Any non-empty value enables them, so `set DISABLE_TELEMETRY 0` would
    # disable telemetry - the opposite of how it reads. The kind is what lets
    # the service reject that.
    assert CLAUDE_CODE_ENV_VARS["DISABLE_TELEMETRY"].kind is Kind.PRESENCE
    assert CLAUDE_CODE_ENV_VARS["DISABLE_ERROR_REPORTING"].kind is Kind.PRESENCE


def test_autocompact_carries_its_one_way_note():
    var = CLAUDE_CODE_ENV_VARS["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"]
    assert var.note is not None
    assert "lower" in var.note
