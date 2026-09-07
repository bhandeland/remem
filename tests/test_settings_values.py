from __future__ import annotations

import pytest

from remem.agents.claude_code.env_vars import CLAUDE_CODE_ENV_VARS
from remem.services.settings import (
    REMEM_VARS,
    InvalidValue,
    Target,
    coerce,
    parse_duration,
)


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
    assert coerce(var, "10m", Target.AGENT) == "600000"


def test_a_non_duration_key_rejects_a_suffix():
    # BASH_MAX_OUTPUT_LENGTH counts characters, not milliseconds, so "10m"
    # is a mistake rather than shorthand.
    var = CLAUDE_CODE_ENV_VARS["BASH_MAX_OUTPUT_LENGTH"]
    with pytest.raises(InvalidValue):
        coerce(var, "10m", Target.AGENT)


def test_an_int_below_the_minimum_is_refused():
    var = CLAUDE_CODE_ENV_VARS["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"]
    with pytest.raises(InvalidValue) as exc:
        coerce(var, "0", Target.AGENT)
    assert "1" in str(exc.value) and "100" in str(exc.value)


def test_an_int_above_the_maximum_is_refused():
    var = CLAUDE_CODE_ENV_VARS["BASH_MAX_OUTPUT_LENGTH"]
    with pytest.raises(InvalidValue):
        coerce(var, "150001", Target.AGENT)


def test_a_non_duration_int_strips_whitespace_before_the_isdigit_check():
    # Same bug as parse_duration's fast path (raw.isdigit() is False on
    # whitespace-padded input), just in the plain-integer branch of coerce.
    # Left unfixed, a duration key would tolerate " 600000 " while a
    # non-duration key like this one rejected the identical-looking value.
    var = CLAUDE_CODE_ENV_VARS["BASH_MAX_OUTPUT_LENGTH"]
    assert coerce(var, " 30000 ", Target.AGENT) == "30000"


def test_a_presence_key_refuses_zero_and_points_at_unset():
    # The trap: any non-empty value enables the disabling, so "0" would
    # disable telemetry rather than re-enable it.
    var = CLAUDE_CODE_ENV_VARS["DISABLE_TELEMETRY"]
    with pytest.raises(InvalidValue) as exc:
        coerce(var, "0", Target.AGENT)
    assert "unset" in str(exc.value)


def test_a_presence_key_accepts_a_truthy_value():
    var = CLAUDE_CODE_ENV_VARS["DISABLE_TELEMETRY"]
    assert coerce(var, "1", Target.AGENT) == "1"


def test_a_bool_key_normalises_true_and_false():
    var = CLAUDE_CODE_ENV_VARS["CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR"]
    assert coerce(var, "true", Target.AGENT) == "1"
    assert coerce(var, "0", Target.AGENT) == "0"


def test_an_empty_string_is_allowed_on_an_agent_key():
    # Claude Code's documented way to neutralise a shell variable the user
    # does not control: "VAR": "" in the settings file. The remem side of
    # this rule is the pair of tests at the foot of the file.
    var = CLAUDE_CODE_ENV_VARS["BASH_DEFAULT_TIMEOUT_MS"]
    assert coerce(var, "", Target.AGENT) == ""


def test_a_float_key_rejects_a_non_number():
    # REMEM_FUZZY_THRESHOLD advertises "0 < t <= 1" in its help. Declared as
    # a plain string it enforced none of it: the junk reached config.toml and
    # config.load() then silently fell back to the default - the exact
    # accepted-then-ignored write this command exists to prevent.
    var = REMEM_VARS["REMEM_FUZZY_THRESHOLD"]
    with pytest.raises(InvalidValue) as exc:
        coerce(var, "not-a-number", Target.REMEM)
    assert "REMEM_FUZZY_THRESHOLD" in str(exc.value)


def test_a_float_key_rejects_a_value_above_the_maximum():
    var = REMEM_VARS["REMEM_FUZZY_THRESHOLD"]
    with pytest.raises(InvalidValue) as exc:
        coerce(var, "5", Target.REMEM)
    # The constraint is stated, not just the refusal.
    assert "1" in str(exc.value) and "0" in str(exc.value)


def test_a_float_key_rejects_its_exclusive_lower_bound():
    # 0 is not merely below the range, it is the boundary: a threshold of 0
    # matches everything, which is why config.load() throws it away too.
    var = REMEM_VARS["REMEM_FUZZY_THRESHOLD"]
    with pytest.raises(InvalidValue):
        coerce(var, "0", Target.REMEM)


def test_a_float_key_accepts_a_value_inside_the_range():
    var = REMEM_VARS["REMEM_FUZZY_THRESHOLD"]
    assert coerce(var, "0.45", Target.REMEM) == "0.45"
    assert coerce(var, "1", Target.REMEM) == "1.0"


def test_a_remem_numeric_key_refuses_an_empty_string():
    # Empty string is Claude Code's documented way to neutralise a shell
    # export. remem's own file has the opposite precedence - the environment
    # already wins - so an empty value neutralises nothing there and is only
    # ever thrown away by config.load(). Refuse it and name the alternative.
    var = REMEM_VARS["REMEM_MAX_CHARS"]
    with pytest.raises(InvalidValue) as exc:
        coerce(var, "", Target.REMEM)
    assert "unset" in str(exc.value)


def test_a_remem_string_key_still_accepts_an_empty_string():
    # Only the numeric keys are refused: an empty extract model or DSN is a
    # legitimate way to blank a file value, and config.load() has its own
    # fallback for it.
    var = REMEM_VARS["REMEM_EXTRACT_MODEL"]
    assert coerce(var, "", Target.REMEM) == ""
