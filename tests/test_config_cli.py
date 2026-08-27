from __future__ import annotations

import json
import tomllib

from typer.testing import CliRunner

from remem.cli import app

runner = CliRunner()


def _env(tmp_path, **extra):
    """Point both targets at a temp dir and keep the real home untouched."""
    return {
        "REMEM_CONFIG": str(tmp_path / "config.toml"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
        **extra,
    }


def test_set_writes_a_claude_code_key(tmp_path):
    result = runner.invoke(
        app,
        ["config", "set", "BASH_DEFAULT_TIMEOUT_MS", "10m"],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    settings = json.loads((tmp_path / "claude" / "settings.json").read_text())
    assert settings["env"]["BASH_DEFAULT_TIMEOUT_MS"] == "600000"
    # The resolved value is echoed so 10m is visibly 600000.
    assert "600000" in result.stdout


def test_set_writes_a_remem_key(tmp_path):
    result = runner.invoke(
        app, ["config", "set", "REMEM_MAX_CHARS", "8000"], env=_env(tmp_path)
    )
    assert result.exit_code == 0
    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert data["max_chars"] == 8000


def test_set_rejects_an_unknown_key_with_a_nonzero_exit(tmp_path):
    result = runner.invoke(
        app, ["config", "set", "NOPE", "1"], env=_env(tmp_path)
    )
    assert result.exit_code == 1
    assert "NOPE" in result.output


def test_set_leaves_the_file_untouched_when_the_value_is_refused(tmp_path):
    path = tmp_path / "claude" / "settings.json"
    runner.invoke(
        app,
        ["config", "set", "BASH_MAX_OUTPUT_LENGTH", "30000"],
        env=_env(tmp_path),
    )
    before = path.read_bytes()

    result = runner.invoke(
        app,
        ["config", "set", "BASH_MAX_OUTPUT_LENGTH", "999999"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    assert path.read_bytes() == before


def test_set_warns_when_an_export_shadows_a_remem_key(tmp_path):
    result = runner.invoke(
        app,
        ["config", "set", "REMEM_MAX_CHARS", "8000"],
        env=_env(tmp_path, REMEM_MAX_CHARS="999"),
    )
    assert result.exit_code == 0
    assert "will not take effect" in result.output


def test_get_prints_the_effective_value(tmp_path):
    runner.invoke(
        app, ["config", "set", "REMEM_MAX_CHARS", "8000"], env=_env(tmp_path)
    )
    result = runner.invoke(
        app, ["config", "get", "REMEM_MAX_CHARS"], env=_env(tmp_path)
    )
    assert result.exit_code == 0
    assert "8000" in result.stdout


def test_unset_removes_the_key(tmp_path):
    runner.invoke(
        app,
        ["config", "set", "BASH_DEFAULT_TIMEOUT_MS", "600000"],
        env=_env(tmp_path),
    )
    result = runner.invoke(
        app, ["config", "unset", "BASH_DEFAULT_TIMEOUT_MS"], env=_env(tmp_path)
    )
    assert result.exit_code == 0
    settings = json.loads((tmp_path / "claude" / "settings.json").read_text())
    assert "BASH_DEFAULT_TIMEOUT_MS" not in settings["env"]


def test_list_shows_keys_values_and_sources(tmp_path):
    runner.invoke(
        app, ["config", "set", "REMEM_MAX_CHARS", "8000"], env=_env(tmp_path)
    )
    result = runner.invoke(app, ["config", "list"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "REMEM_MAX_CHARS" in result.stdout
    assert "8000" in result.stdout
    assert "file" in result.stdout
    assert "BASH_DEFAULT_TIMEOUT_MS" in result.stdout
    assert "default" in result.stdout


def test_list_surfaces_a_variables_note(tmp_path):
    result = runner.invoke(app, ["config", "list"], env=_env(tmp_path))
    assert "lower" in result.stdout


def test_an_unknown_agent_is_refused_cleanly(tmp_path):
    # Not a traceback: registry.get already names what is registered.
    result = runner.invoke(
        app, ["config", "list", "--agent", "nope"], env=_env(tmp_path)
    )
    assert result.exit_code == 1
    assert "nope" in result.output
    assert "claude-code" in result.output


def test_config_works_with_postgres_unreachable(tmp_path):
    # No command in this group may open a session. A bad DSN must not matter.
    result = runner.invoke(
        app,
        ["config", "list"],
        env=_env(tmp_path, REMEM_DSN="postgresql://nobody@127.0.0.1:1/none"),
    )
    assert result.exit_code == 0


def test_set_strips_whitespace_on_a_non_duration_integer(tmp_path):
    # coerce's plain-integer branch (var.duration is False) had the same
    # isdigit()-without-strip gap as parse_duration - fixed alongside it so a
    # duration key and a non-duration key don't disagree on an
    # identical-looking padded value.
    result = runner.invoke(
        app,
        ["config", "set", "BASH_MAX_OUTPUT_LENGTH", " 30000 "],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    settings = json.loads((tmp_path / "claude" / "settings.json").read_text())
    assert settings["env"]["BASH_MAX_OUTPUT_LENGTH"] == "30000"


def test_set_strips_whitespace_before_parsing_a_duration(tmp_path):
    # parse_duration's plain-integer fast path uses raw.isdigit() without
    # stripping, so an unstripped " 600000 " misses it and falls into the
    # suffix regex, which then rejects it. CLI input is the one path that can
    # actually carry incidental whitespace (a copy-pasted value, a quoted
    # shell argument) so this is exercised here rather than in Task 3.
    result = runner.invoke(
        app,
        ["config", "set", "BASH_DEFAULT_TIMEOUT_MS", " 600000 "],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    settings = json.loads((tmp_path / "claude" / "settings.json").read_text())
    assert settings["env"]["BASH_DEFAULT_TIMEOUT_MS"] == "600000"


def test_set_refuses_a_non_numeric_fuzzy_threshold(tmp_path):
    # The end-to-end shape of the bug: the value used to be written verbatim
    # and then discarded by config.load(), so `set` reported success and
    # nothing changed.
    result = runner.invoke(
        app,
        ["config", "set", "REMEM_FUZZY_THRESHOLD", "not-a-number"],
        env=_env(tmp_path),
    )
    assert result.exit_code == 1
    assert not (tmp_path / "config.toml").exists()


def test_set_writes_a_fuzzy_threshold_as_a_toml_float(tmp_path):
    # A quoted "0.45" would be thrown away by config.load()'s float() guard
    # in exactly the way a bare string value was.
    result = runner.invoke(
        app,
        ["config", "set", "REMEM_FUZZY_THRESHOLD", "0.45"],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert data["fuzzy_threshold"] == 0.45


def test_set_refuses_an_empty_value_on_a_remem_integer(tmp_path):
    result = runner.invoke(
        app, ["config", "set", "REMEM_MAX_CHARS", ""], env=_env(tmp_path)
    )
    assert result.exit_code == 1
    assert "unset" in result.output
    assert not (tmp_path / "config.toml").exists()
