from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Mapping

import pytest
from typer.testing import CliRunner

import remem.agents.registry as registry
from remem.agents.base import EnvVar, Kind
from remem.agents.claude_code.env_vars import CLAUDE_CODE_ENV_VARS
from remem.cli import app
from remem.services.settings import REMEM_VARS

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """Strip every settable key from the real process environment.

    CliRunner's `env=` *merges* into os.environ rather than replacing it, and
    a CLI frontend legitimately reads os.environ - so without this a
    developer who exports REMEM_MAX_CHARS in their shell changes what these
    tests see (that key's source becomes "environment"). The repo's rule is
    that the environment is injected, never inherited, and for a CLI test
    that means clearing the inherited half first.
    """
    for key in list(REMEM_VARS) + list(CLAUDE_CODE_ENV_VARS):
        monkeypatch.delenv(key, raising=False)


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
    result = runner.invoke(app, ["config", "set", "NOPE", "1"], env=_env(tmp_path))
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
    runner.invoke(app, ["config", "set", "REMEM_MAX_CHARS", "8000"], env=_env(tmp_path))
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
    runner.invoke(app, ["config", "set", "REMEM_MAX_CHARS", "8000"], env=_env(tmp_path))
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


def test_set_reports_where_the_remem_backup_went(tmp_path):
    # Rewriting config.toml loses comments and formatting. The backup lands
    # beside the file under a timestamped name the user has no reason to
    # guess, so a safety net nobody is told about is most of the way to no
    # safety net at all.
    path = tmp_path / "config.toml"
    path.write_text("# hand written\nmax_chars = 4000\n")
    result = runner.invoke(
        app, ["config", "set", "REMEM_MAX_CHARS", "8000"], env=_env(tmp_path)
    )
    assert result.exit_code == 0
    backups = [p for p in tmp_path.iterdir() if ".bak" in p.name]
    assert len(backups) == 1
    assert str(backups[0]) in result.stdout


def test_set_reports_where_the_agent_backup_went(tmp_path):
    path = tmp_path / "claude" / "settings.json"
    path.parent.mkdir()
    path.write_text('{"env": {"BASH_MAX_OUTPUT_LENGTH": "1000"}}')
    result = runner.invoke(
        app,
        ["config", "set", "BASH_DEFAULT_TIMEOUT_MS", "10m"],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    backups = [p for p in path.parent.iterdir() if ".bak" in p.name]
    assert len(backups) == 1
    assert str(backups[0]) in result.stdout


def test_unset_reports_where_the_backup_went(tmp_path):
    runner.invoke(app, ["config", "set", "REMEM_MAX_CHARS", "8000"], env=_env(tmp_path))
    result = runner.invoke(
        app, ["config", "unset", "REMEM_MAX_CHARS"], env=_env(tmp_path)
    )
    assert result.exit_code == 0
    assert "Backed up to" in result.stdout


def test_set_says_nothing_about_a_backup_when_there_was_no_file(tmp_path):
    # Nothing to lose on a first write, so nothing to report.
    result = runner.invoke(
        app, ["config", "set", "REMEM_MAX_CHARS", "8000"], env=_env(tmp_path)
    )
    assert result.exit_code == 0
    assert "Backed up" not in result.stdout


class _OtherAgent:
    """A second registered adapter, with its own table and its own file."""

    name = "other"

    def env_settings(self) -> Mapping[str, EnvVar]:
        return {
            "OTHER_TIMEOUT_MS": EnvVar(
                "OTHER_TIMEOUT_MS",
                Kind.INT,
                "Another agent's timeout.",
                minimum=1,
            )
        }

    def settings_path(self, home: Path, env: Mapping[str, str]) -> Path:
        return Path(env["OTHER_SETTINGS"])


@pytest.fixture
def other_agent(monkeypatch):
    real = registry.discover()
    monkeypatch.setattr(registry, "discover", lambda: {**real, "other": _OtherAgent})


def test_set_on_a_second_agent_writes_that_agents_file(
    tmp_path, other_agent, monkeypatch
):
    # Before the probes moved into the service, --agent picked the table but
    # the settings path was always Claude Code's, so this value would have
    # landed in ~/.claude/settings.json - silent, and wrong in the direction
    # that corrupts another tool's config.
    other = tmp_path / "other-settings.json"
    monkeypatch.setenv("OTHER_SETTINGS", str(other))
    result = runner.invoke(
        app,
        ["config", "set", "--agent", "other", "OTHER_TIMEOUT_MS", "10"],
        env=_env(tmp_path, OTHER_SETTINGS=str(other)),
    )
    assert result.exit_code == 0
    assert json.loads(other.read_text())["env"]["OTHER_TIMEOUT_MS"] == "10"
    assert not (tmp_path / "claude" / "settings.json").exists()


def test_a_claude_code_key_is_unknown_to_another_agent(
    tmp_path, other_agent, monkeypatch
):
    other = tmp_path / "other-settings.json"
    monkeypatch.setenv("OTHER_SETTINGS", str(other))
    result = runner.invoke(
        app,
        ["config", "set", "--agent", "other", "BASH_MAX_OUTPUT_LENGTH", "100"],
        env=_env(tmp_path, OTHER_SETTINGS=str(other)),
    )
    assert result.exit_code == 1
    assert not other.exists()


def test_an_unknown_key_error_is_not_wrapped_in_quotes(tmp_path):
    # UnknownSetting subclasses KeyError, whose str() is the repr of its
    # argument. Stripping quotes off both ends of that would also eat a
    # closing quote from a message that legitimately ends in a quoted key.
    result = runner.invoke(app, ["config", "get", "NOPE"], env=_env(tmp_path))
    assert result.exit_code == 1
    line = result.output.strip().splitlines()[0]
    assert line.startswith("unknown setting")
