from __future__ import annotations

import pytest

from remem.agents.claude_code.env_vars import CLAUDE_CODE_ENV_VARS
from remem.services.settings import InvalidValue, coerce, parse_duration


@pytest.mark.parametrize(
    "raw,expected",
    [("10m", 600000), ("30s", 30000), ("500ms", 500), ("600000", 600000)],
)
def test_parse_duration_accepts_suffixes_and_plain_milliseconds(raw, expected):
    assert parse_duration(raw) == expected


def test_parse_duration_rejects_an_unknown_suffix():
    with pytest.raises(InvalidValue):
        parse_duration("10h")


def test_a_duration_key_accepts_a_suffix():
    var = CLAUDE_CODE_ENV_VARS["BASH_DEFAULT_TIMEOUT_MS"]
    assert coerce(var, "10m") == "600000"


def test_a_non_duration_key_rejects_a_suffix():
    # BASH_MAX_OUTPUT_LENGTH counts characters, not milliseconds, so "10m"
    # is a mistake rather than shorthand.
    var = CLAUDE_CODE_ENV_VARS["BASH_MAX_OUTPUT_LENGTH"]
    with pytest.raises(InvalidValue):
        coerce(var, "10m")


def test_an_int_below_the_minimum_is_refused():
    var = CLAUDE_CODE_ENV_VARS["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"]
    with pytest.raises(InvalidValue) as exc:
        coerce(var, "0")
    assert "1" in str(exc.value) and "100" in str(exc.value)


def test_an_int_above_the_maximum_is_refused():
    var = CLAUDE_CODE_ENV_VARS["BASH_MAX_OUTPUT_LENGTH"]
    with pytest.raises(InvalidValue):
        coerce(var, "150001")


def test_a_presence_key_refuses_zero_and_points_at_unset():
    # The trap: any non-empty value enables the disabling, so "0" would
    # disable telemetry rather than re-enable it.
    var = CLAUDE_CODE_ENV_VARS["DISABLE_TELEMETRY"]
    with pytest.raises(InvalidValue) as exc:
        coerce(var, "0")
    assert "unset" in str(exc.value)


def test_a_presence_key_accepts_a_truthy_value():
    var = CLAUDE_CODE_ENV_VARS["DISABLE_TELEMETRY"]
    assert coerce(var, "1") == "1"


def test_a_bool_key_normalises_true_and_false():
    var = CLAUDE_CODE_ENV_VARS["CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR"]
    assert coerce(var, "true") == "1"
    assert coerce(var, "0") == "0"


def test_an_empty_string_is_always_allowed():
    # Claude Code's documented way to neutralise a shell variable the user
    # does not control: "VAR": "" in the settings file.
    var = CLAUDE_CODE_ENV_VARS["BASH_DEFAULT_TIMEOUT_MS"]
    assert coerce(var, "") == ""
