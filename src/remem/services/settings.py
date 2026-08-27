"""Reads and writes remem's own settings and an agent's environment block.

Every policy decision for `remem config` lives here: which file a key belongs
to, whether a value is legal, and whether a write will actually take effect.
The adapter supplies the table of what exists; this module decides what may
be done with it.

The one service that touches no store - `remem config` must work with
Postgres down.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping

import tomli_w

from remem import config as remem_config
from remem import jsonfile
from remem.agents.base import EnvVar, Kind


class InvalidValue(ValueError):
    """The value is not legal for this setting. Nothing has been written."""


_DURATION = re.compile(r"^(\d+)(ms|s|m)$")
_MULTIPLIER = {"ms": 1, "s": 1000, "m": 60000}

# "0"/"false" on a Kind.BOOL key is meaningful; on a Kind.PRESENCE key it is
# a trap, because Claude Code enables those on any non-empty value.
_FALSEY = {"0", "false", "no", "off"}
_TRUTHY = {"1", "true", "yes", "on"}


def parse_duration(raw: str) -> int:
    """Milliseconds from 10m / 30s / 500ms, or from a plain integer."""
    # Strip before the fast path, not just before the regex: CLI input can
    # carry incidental whitespace (a copy-pasted value, a quoted shell
    # argument) that `str.isdigit()` treats as non-digit, sending a plain
    # " 600000 " into the suffix regex below, where it is rejected outright.
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    match = _DURATION.match(raw.lower())
    if not match:
        raise InvalidValue(
            f"{raw!r} is not a duration. Use milliseconds, or a suffix: "
            "500ms, 30s, 10m."
        )
    return int(match.group(1)) * _MULTIPLIER[match.group(2)]


def coerce(var: EnvVar, raw: str) -> str:
    """Validate raw against var and return the string to write.

    Raises InvalidValue rather than writing something approximate. Callers
    must call this before touching a file, so that a rejected value leaves
    the file byte-identical.
    """
    # An empty string is never a type error: it is Claude Code's documented
    # way to override a shell variable the user cannot otherwise control.
    # `unset` is the separate operation that removes the key.
    if raw == "":
        return ""

    if var.kind is Kind.PRESENCE:
        if raw.strip().lower() in _FALSEY:
            raise InvalidValue(
                f"{var.name} is enabled by presence, so {raw!r} would still "
                f"enable it. Use `remem config unset {var.name}` instead."
            )
        return raw

    if var.kind is Kind.BOOL:
        value = raw.strip().lower()
        if value in _TRUTHY:
            return "1"
        if value in _FALSEY:
            return "0"
        raise InvalidValue(f"{var.name} takes a boolean, not {raw!r}.")

    if var.kind is Kind.INT:
        if var.duration:
            number = parse_duration(raw)
        elif raw.isdigit():
            number = int(raw)
        else:
            raise InvalidValue(f"{var.name} takes a whole number, not {raw!r}.")
        # Both bounds are reported on every violation, not just the one that
        # was crossed - the caller sees the full legal range in one message
        # instead of having to trigger the other error to learn it.
        if var.minimum is not None and number < var.minimum:
            raise InvalidValue(
                f"{var.name} must be between {var.minimum} and "
                f"{var.maximum if var.maximum is not None else 'unbounded'}; "
                f"got {number}."
            )
        if var.maximum is not None and number > var.maximum:
            raise InvalidValue(
                f"{var.name} must be between "
                f"{var.minimum if var.minimum is not None else 'unbounded'} "
                f"and {var.maximum}; got {number}."
            )
        return str(number)

    return raw


class UnknownSetting(KeyError):
    """No such setting in either table."""


class NotSettable(ValueError):
    """A real variable that must not be written into a settings file."""


class Target(StrEnum):
    REMEM = "remem"
    AGENT = "agent"


# Each of these names the file that would store it, so writing one inside
# that file is a chicken-and-egg that reads as broken. The Claude Code docs
# say the same about CLAUDE_CONFIG_DIR: set it in the shell.
BOOTSTRAP_VARS = {"REMEM_CONFIG", "CLAUDE_CONFIG_DIR"}

#: remem's own settings, keyed by env-var spelling. Derived from config.py so
#: that a new setting there shows up in `remem config list` without a second
#: edit here; only the help text lives in this table.
REMEM_VARS: Mapping[str, EnvVar] = {
    "REMEM_DSN": EnvVar(
        "REMEM_DSN", Kind.STR, "Postgres connection string.",
        default=remem_config.DEFAULT_DSN,
    ),
    "REMEM_USER_ID": EnvVar(
        "REMEM_USER_ID", Kind.STR, "Handle entries are attributed to.",
        default=remem_config.DEFAULT_HANDLE,
    ),
    "REMEM_MAX_CHARS": EnvVar(
        "REMEM_MAX_CHARS", Kind.INT, "Cap on a rendered context block.",
        minimum=1, default=str(remem_config.DEFAULT_MAX_CHARS),
    ),
    "REMEM_FUZZY_THRESHOLD": EnvVar(
        "REMEM_FUZZY_THRESHOLD", Kind.STR,
        "Trigram similarity floor for the fuzzy fallback (0 < t <= 1).",
        default=str(remem_config.DEFAULT_FUZZY_THRESHOLD),
    ),
    "REMEM_CAPTURE_MODEL": EnvVar(
        "REMEM_CAPTURE_MODEL", Kind.STR,
        "Model used to distil captured sessions.",
        default=remem_config.DEFAULT_CAPTURE_MODEL,
    ),
    "REMEM_TURN_WARN_AT": EnvVar(
        "REMEM_TURN_WARN_AT", Kind.INT,
        "Turn count at which the handoff reminder first fires.",
        minimum=1, default=str(remem_config.DEFAULT_TURN_WARN_AT),
    ),
    "REMEM_TURN_WARN_EVERY": EnvVar(
        "REMEM_TURN_WARN_EVERY", Kind.INT,
        "Turns between repeat handoff reminders.",
        minimum=1, default=str(remem_config.DEFAULT_TURN_WARN_EVERY),
    ),
}

#: config.toml uses unprefixed keys; user_handle is the one that is not just
#: the lowercased suffix.
_FILE_KEYS = {name: name.removeprefix("REMEM_").lower() for name in REMEM_VARS}
_FILE_KEYS["REMEM_USER_ID"] = "user_handle"
_BY_FILE_KEY = {v: k for k, v in _FILE_KEYS.items()}


def file_key(key: str) -> str:
    """The config.toml spelling of a remem setting, from either input form."""
    if key in _FILE_KEYS:
        return _FILE_KEYS[key]
    if key in _BY_FILE_KEY:
        return key
    raise UnknownSetting(key)


def route(key: str, table: Mapping[str, EnvVar]) -> tuple[Target, EnvVar]:
    """Which file this key belongs to, and its definition.

    Refuses rather than guessing: an unrecognised key is far more often a
    typo than a variable remem has not heard of, and silently writing it
    would leave a dead entry that looks like a working setting.
    """
    if key in BOOTSTRAP_VARS:
        raise NotSettable(
            f"{key} names the file that would store it, so it cannot be set "
            "there. Export it from your shell instead."
        )
    if key in REMEM_VARS:
        return Target.REMEM, REMEM_VARS[key]
    if key in _BY_FILE_KEY:
        return Target.REMEM, REMEM_VARS[_BY_FILE_KEY[key]]
    if key in table:
        return Target.AGENT, table[key]
    supported = ", ".join(sorted(set(REMEM_VARS) | set(table)))
    raise UnknownSetting(f"unknown setting '{key}'. Supported: {supported}")


def write_remem(path: Path, key: str, value: str | None) -> None:
    """Set or unset one key in remem's config.toml.

    Rewriting the file loses comments and formatting, which is why it is
    backed up first. tomli-w rather than a hand-rolled writer because the DSN
    can hold a password containing quotes or backslashes, and TOML escaping
    is the wrong thing to be clever about.
    """
    data: dict = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError:
            # Same posture as config.load(): a broken file must not be a
            # dead end. It is backed up below before being replaced.
            data = {}
        jsonfile.backup_once(path, set())

    name = file_key(key)
    if value is None:
        data.pop(name, None)
    else:
        env_key = _BY_FILE_KEY.get(name, name)
        var = REMEM_VARS[env_key]
        # Write the natural TOML type so the file reads the way a human
        # would have written it, and `get` round-trips what was set.
        data[name] = int(value) if var.kind is Kind.INT and value else value

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(data))


@dataclass(frozen=True, slots=True)
class Setting:
    key: str
    value: str | None
    #: "environment", "file", or "default" - the whole point of `list`.
    source: str
    var: EnvVar
    target: Target


def write_agent(path: Path, key: str, value: str | None) -> None:
    """Set or unset one key in the env block of an agent's settings.json."""
    backed_up: set[Path] = set()
    data, _ = jsonfile.read_json(path, backed_up)
    env_block = data.get("env")
    if not isinstance(env_block, dict):
        # settings.json is a file remem does not own, and every other touch
        # of it degrades rather than raising. A hand-corrupted file can have
        # "env" set to something other than a dict (e.g. "env": "yes"); a
        # bare setdefault would hand back that non-dict value and crash on
        # the next line. Replace it with a fresh dict instead - the same
        # posture as write_remem's "broken file must not be a dead end".
        env_block = {}
        data["env"] = env_block
    if value is None:
        env_block.pop(key, None)
    else:
        env_block[key] = value
    jsonfile.write_json(path, data, backed_up)


def shadow_warning(
    target: Target, key: str, env: Mapping[str, str]
) -> str | None:
    """Whether the value just written will actually be the one in effect.

    The two targets resolve in opposite directions, which is the single most
    confusing thing about this command:

        remem        environment beats config.toml
        Claude Code  settings.json beats the environment

    So the same situation - the key is also exported - is a silent no-op on
    one side and the intended behaviour on the other. Saying nothing would
    leave the user staring at a correctly written file that changed nothing.
    """
    if key not in env:
        return None
    if target is Target.REMEM:
        return (
            f"{key} is set in your environment, which takes precedence over "
            f"the config file, so this change will not take effect until you "
            f"unset it."
        )
    return (
        f"{key} is also set in your environment. The settings file overrides "
        f"it, so the value just written is the one that will be used."
    )


def _agent_env(path: Path) -> dict:
    data, _ = jsonfile.read_json(path, set())
    block = data.get("env")
    return block if isinstance(block, dict) else {}


def list_settings(
    remem_path: Path,
    agent_path: Path,
    table: Mapping[str, EnvVar],
    env: Mapping[str, str],
) -> list[Setting]:
    """Every settable key, its effective value, and where that value came from.

    This is the feature that justifies the command existing alongside
    Claude Code's own /config: one view over both tools, with each side's
    precedence rule already applied.
    """
    rows: list[Setting] = []

    file_data: dict = {}
    if remem_path.exists():
        try:
            file_data = tomllib.loads(remem_path.read_text())
        except tomllib.TOMLDecodeError:
            file_data = {}

    for key, var in REMEM_VARS.items():
        # Environment first: config.load() picks the env var over the file.
        if key in env:
            rows.append(Setting(key, env[key], "environment", var, Target.REMEM))
            continue
        name = _FILE_KEYS[key]
        if name in file_data:
            rows.append(
                Setting(key, str(file_data[name]), "file", var, Target.REMEM)
            )
            continue
        rows.append(Setting(key, var.default, "default", var, Target.REMEM))

    agent_block = _agent_env(agent_path)
    for key, var in table.items():
        # File first: for Claude Code the settings file beats the export.
        if key in agent_block:
            rows.append(
                Setting(key, agent_block[key], "file", var, Target.AGENT)
            )
            continue
        if key in env:
            rows.append(
                Setting(key, env[key], "environment", var, Target.AGENT)
            )
            continue
        rows.append(Setting(key, var.default, "default", var, Target.AGENT))

    return rows
