from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from saddlebag.agents.claude_code.env_vars import CLAUDE_CODE_ENV_VARS
from saddlebag.services.settings import (
    NotSettable,
    Target,
    UnknownSetting,
    file_key,
    route,
    write_saddlebag,
)

TABLE = CLAUDE_CODE_ENV_VARS


def test_a_saddlebag_key_routes_to_saddlebags_own_config() -> None:
    target, var = route("BAG_MAX_CHARS", TABLE)
    assert target is Target.SADDLEBAG
    assert var.name == "BAG_MAX_CHARS"


def test_the_unprefixed_spelling_routes_the_same_way() -> None:
    # saddlebag's file keys are unprefixed while its documentation is written in
    # env-var spelling. Both must work as input.
    assert route("max_chars", TABLE)[0] is Target.SADDLEBAG


def test_both_spellings_reach_the_same_file_key() -> None:
    assert file_key("BAG_MAX_CHARS") == "max_chars"
    assert file_key("max_chars") == "max_chars"


def test_a_claude_code_key_routes_to_the_agent() -> None:
    target, var = route("BASH_DEFAULT_TIMEOUT_MS", TABLE)
    assert target is Target.AGENT
    assert var.name == "BASH_DEFAULT_TIMEOUT_MS"


def test_an_unknown_key_is_refused_and_names_what_is_supported() -> None:
    with pytest.raises(UnknownSetting) as exc:
        route("NONSENSE_KEY", TABLE)
    message = str(exc.value)
    assert "NONSENSE_KEY" in message
    assert "BASH_DEFAULT_TIMEOUT_MS" in message
    assert "BAG_MAX_CHARS" in message


@pytest.mark.parametrize("key", ["CLAUDE_CONFIG_DIR", "BAG_CONFIG"])
def test_the_two_bootstrap_variables_are_refused(key: str) -> None:
    # Each names the file that would store it. The error has to send the
    # user to the shell rather than leave them looking for a typo.
    with pytest.raises(NotSettable) as exc:
        route(key, TABLE)
    assert "shell" in str(exc.value)


def test_write_saddlebag_creates_the_file_with_the_unprefixed_key(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    write_saddlebag(path, "BAG_MAX_CHARS", "8000")
    assert tomllib.loads(path.read_text())["max_chars"] == 8000


def test_write_saddlebag_preserves_other_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('capture_model = "opus"\n')
    write_saddlebag(path, "BAG_MAX_CHARS", "8000")
    data = tomllib.loads(path.read_text())
    assert data["capture_model"] == "opus"
    assert data["max_chars"] == 8000


def test_write_saddlebag_round_trips_a_dsn_with_quotes_and_backslashes(
    tmp_path: Path,
) -> None:
    # The case that justified taking tomli-w as a dependency rather than
    # hand-rolling a writer: TOML string escaping is not worth being clever
    # about when a password can contain anything.
    path = tmp_path / "config.toml"
    nasty = r'postgresql://u:pa"ss\word@localhost:5433/saddlebag'
    write_saddlebag(path, "BAG_DSN", nasty)
    assert tomllib.loads(path.read_text())["dsn"] == nasty


def test_write_saddlebag_backs_up_before_rewriting(tmp_path: Path) -> None:
    # A rewrite loses comments and formatting, so the previous file has to
    # survive somewhere.
    path = tmp_path / "config.toml"
    path.write_text("# hand written\ncapture_model = 'opus'\n")
    write_saddlebag(path, "BAG_MAX_CHARS", "8000")
    backups = list(tmp_path.glob("config.toml.bak*"))
    assert len(backups) == 1
    assert "# hand written" in backups[0].read_text()


def test_write_saddlebag_unsets_with_none(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("max_chars = 8000\ncapture_model = 'opus'\n")
    write_saddlebag(path, "BAG_MAX_CHARS", None)
    data = tomllib.loads(path.read_text())
    assert "max_chars" not in data
    assert data["capture_model"] == "opus"


def test_int_settings_are_written_as_toml_integers(tmp_path: Path) -> None:
    # Written as a string, config.load()'s int() would still work, but
    # `bag config get` would round-trip 8000 as "8000" and the file would
    # not match what a human would have written.
    path = tmp_path / "config.toml"
    write_saddlebag(path, "BAG_MAX_CHARS", "8000")
    assert "max_chars = 8000" in path.read_text()
