from __future__ import annotations

import tomllib

import pytest

from remem.agents.claude_code.env_vars import CLAUDE_CODE_ENV_VARS
from remem.services.settings import (
    NotSettable,
    Target,
    UnknownSetting,
    file_key,
    route,
    write_remem,
)

TABLE = CLAUDE_CODE_ENV_VARS


def test_a_remem_key_routes_to_remems_own_config():
    target, var = route("REMEM_MAX_CHARS", TABLE)
    assert target is Target.REMEM
    assert var.name == "REMEM_MAX_CHARS"


def test_the_unprefixed_spelling_routes_the_same_way():
    # remem's file keys are unprefixed while its documentation is written in
    # env-var spelling. Both must work as input.
    assert route("max_chars", TABLE)[0] is Target.REMEM


def test_both_spellings_reach_the_same_file_key():
    assert file_key("REMEM_MAX_CHARS") == "max_chars"
    assert file_key("max_chars") == "max_chars"


def test_a_claude_code_key_routes_to_the_agent():
    target, var = route("BASH_DEFAULT_TIMEOUT_MS", TABLE)
    assert target is Target.AGENT
    assert var.name == "BASH_DEFAULT_TIMEOUT_MS"


def test_an_unknown_key_is_refused_and_names_what_is_supported():
    with pytest.raises(UnknownSetting) as exc:
        route("NONSENSE_KEY", TABLE)
    message = str(exc.value)
    assert "NONSENSE_KEY" in message
    assert "BASH_DEFAULT_TIMEOUT_MS" in message
    assert "REMEM_MAX_CHARS" in message


@pytest.mark.parametrize("key", ["CLAUDE_CONFIG_DIR", "REMEM_CONFIG"])
def test_the_two_bootstrap_variables_are_refused(key):
    # Each names the file that would store it. The error has to send the
    # user to the shell rather than leave them looking for a typo.
    with pytest.raises(NotSettable) as exc:
        route(key, TABLE)
    assert "shell" in str(exc.value)


def test_write_remem_creates_the_file_with_the_unprefixed_key(tmp_path):
    path = tmp_path / "config.toml"
    write_remem(path, "REMEM_MAX_CHARS", "8000")
    assert tomllib.loads(path.read_text())["max_chars"] == 8000


def test_write_remem_preserves_other_settings(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('capture_model = "opus"\n')
    write_remem(path, "REMEM_MAX_CHARS", "8000")
    data = tomllib.loads(path.read_text())
    assert data["capture_model"] == "opus"
    assert data["max_chars"] == 8000


def test_write_remem_round_trips_a_dsn_with_quotes_and_backslashes(tmp_path):
    # The case that justified taking tomli-w as a dependency rather than
    # hand-rolling a writer: TOML string escaping is not worth being clever
    # about when a password can contain anything.
    path = tmp_path / "config.toml"
    nasty = r'postgresql://u:pa"ss\word@localhost:5433/remem'
    write_remem(path, "REMEM_DSN", nasty)
    assert tomllib.loads(path.read_text())["dsn"] == nasty


def test_write_remem_backs_up_before_rewriting(tmp_path):
    # A rewrite loses comments and formatting, so the previous file has to
    # survive somewhere.
    path = tmp_path / "config.toml"
    path.write_text("# hand written\ncapture_model = 'opus'\n")
    write_remem(path, "REMEM_MAX_CHARS", "8000")
    backups = list(tmp_path.glob("config.toml.bak*"))
    assert len(backups) == 1
    assert "# hand written" in backups[0].read_text()


def test_write_remem_unsets_with_none(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("max_chars = 8000\ncapture_model = 'opus'\n")
    write_remem(path, "REMEM_MAX_CHARS", None)
    data = tomllib.loads(path.read_text())
    assert "max_chars" not in data
    assert data["capture_model"] == "opus"


def test_int_settings_are_written_as_toml_integers(tmp_path):
    # Written as a string, config.load()'s int() would still work, but
    # `remem config get` would round-trip 8000 as "8000" and the file would
    # not match what a human would have written.
    path = tmp_path / "config.toml"
    write_remem(path, "REMEM_MAX_CHARS", "8000")
    assert "max_chars = 8000" in path.read_text()
