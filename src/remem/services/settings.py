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


def _float_range_error(var: EnvVar, number: float) -> str:
    """The refusal message for a float outside its bounds.

    States the whole constraint rather than the half that was crossed - the
    caller learns the legal range from one error instead of having to trip
    the other one to find the far end.
    """
    parts = []
    if var.minimum is not None:
        gate = "greater than" if var.exclusive_minimum else "at least"
        parts.append(f"{gate} {var.minimum}")
    if var.maximum is not None:
        parts.append(f"at most {var.maximum}")
    return f"{var.name} must be {' and '.join(parts)}; got {number}."


def coerce(var: EnvVar, raw: str, target: Target) -> str:
    """Validate raw against var and return the string to write.

    Raises InvalidValue rather than writing something approximate. Callers
    must call this before touching a file, so that a rejected value leaves
    the file byte-identical.

    `target` is needed only for the empty-string rule below: the two files
    give an empty value opposite meanings, and the value alone cannot say
    which one is being written.
    """
    # An empty string is not a type error in an agent's settings file: it is
    # Claude Code's documented way to override a shell variable the user
    # cannot otherwise control, and `unset` is the separate operation that
    # removes the key.
    #
    # remem's own file resolves the other way - the environment already beats
    # config.toml - so an empty value there neutralises nothing. On a numeric
    # key it is worse than useless: `max_chars = ""` is accepted here and
    # then thrown away by config.load()'s int(), which is the accepted-then-
    # ignored write this whole command exists to prevent. Refuse it and name
    # the operation the user actually wanted. Free-form remem keys (the DSN,
    # the capture model) keep the permissive behaviour: blanking a file value
    # there is meaningful and config.load() has its own fallback for it.
    if raw == "":
        if target is Target.REMEM and var.kind in (Kind.INT, Kind.FLOAT):
            raise InvalidValue(
                f"{var.name} takes a number, so an empty value would be "
                f"ignored. Use `remem config unset {var.name}` instead."
            )
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
        elif raw.strip().isdigit():
            # Same whitespace tolerance as parse_duration, and for the same
            # reason: CLI input can carry incidental whitespace that a bare
            # isdigit() treats as non-numeric. Without this, a duration key
            # and a plain-integer key would disagree on an identical-looking
            # padded value, which is worse than rejecting both.
            number = int(raw.strip())
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

    if var.kind is Kind.FLOAT:
        try:
            number = float(raw.strip())
        except ValueError:
            raise InvalidValue(
                f"{var.name} takes a number, not {raw!r}."
            ) from None
        if var.minimum is not None:
            below = (
                number <= var.minimum
                if var.exclusive_minimum
                else number < var.minimum
            )
            if below:
                raise InvalidValue(_float_range_error(var, number))
        if var.maximum is not None and number > var.maximum:
            raise InvalidValue(_float_range_error(var, number))
        # Normalised rather than echoed back, so that "1" and ".5" reach the
        # file in the same spelling a human would have written, and so that
        # write_remem's float() call cannot fail on something coerce accepted.
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
    # The bounds mirror config.load()'s own guard exactly. Below or at 0 the
    # fuzzy fallback matches everything and above 1 it matches nothing, so
    # config.load() discards anything outside the range - which, while this
    # was a free-form Kind.STR, made `set` a reliable no-op for a bad value.
    "REMEM_FUZZY_THRESHOLD": EnvVar(
        "REMEM_FUZZY_THRESHOLD", Kind.FLOAT,
        "Trigram similarity floor for the fuzzy fallback (0 < t <= 1).",
        minimum=0.0, maximum=1.0, exclusive_minimum=True,
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


def _read_toml(path: Path) -> dict:
    """remem's config.toml, or an empty dict if it is missing or broken.

    Same posture as config.load(): a file remem cannot parse must not be a
    dead end. Both readers below need it, and two copies of a three-line
    try/except is how they drift apart.
    """
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError:
        return {}


def _typed(var: EnvVar, value: str) -> object:
    """The value as the TOML type its kind implies.

    coerce() has already normalised the string, so int()/float() here cannot
    fail on anything that reached this point; an empty value on a numeric
    remem key is refused there rather than written as a string.
    """
    if not value:
        return value
    if var.kind is Kind.INT:
        return int(value)
    if var.kind is Kind.FLOAT:
        return float(value)
    return value


def write_remem(path: Path, key: str, value: str | None) -> Path | None:
    """Set or unset one key in remem's config.toml.

    Rewriting the file loses comments and formatting, which is why it is
    backed up first, and why the backup's path is returned for the caller to
    report. tomli-w rather than a hand-rolled writer because the DSN can hold
    a password containing quotes or backslashes, and TOML escaping is the
    wrong thing to be clever about.
    """
    data = _read_toml(path)
    backed_up: Path | None = None
    if path.exists():
        # A broken file is read as empty and then replaced, which is exactly
        # why this backup matters.
        #
        # A fresh de-duplication set rather than a shared one: this function
        # reads the file itself instead of going through read_json, and
        # writes once per process, so there is exactly one backup per
        # invocation and nothing for a set carried in from outside to
        # suppress.
        backed_up = jsonfile.backup_once(path, set())

    name = file_key(key)
    if value is None:
        data.pop(name, None)
    else:
        env_key = _BY_FILE_KEY.get(name, name)
        var = REMEM_VARS[env_key]
        # Write the natural TOML type so the file reads the way a human
        # would have written it, and `get` round-trips what was set. A
        # quoted number would be as good as no write at all: config.load()
        # coerces with int()/float() and falls back to the default on
        # anything it cannot parse.
        data[name] = _typed(var, value)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(data))
    return backed_up


@dataclass(frozen=True, slots=True)
class Setting:
    key: str
    value: str | None
    #: "environment", "file", or "default" - the whole point of `list`.
    source: str
    var: EnvVar
    target: Target


def write_agent(path: Path, key: str, value: str | None) -> Path | None:
    """Set or unset one key in the env block of an agent's settings.json.

    Returns where the file was backed up, for the caller to report.
    """
    # Back up before reading rather than letting write_json do it at the end.
    # read_json snapshots the file itself when it turns out to be corrupt, and
    # that copy is the one that matters - taking it here means one backup per
    # invocation either way, with a path this function can return.
    backed_up: set[Path] = set()
    made = jsonfile.backup_once(path, backed_up)
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
    return made


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


@dataclass(frozen=True, slots=True)
class Targets:
    """The files one `remem config` invocation reads and writes.

    agent_path is None when the resolved adapter has no env block remem can
    write, in which case table is empty too - the two always travel together.
    """

    remem_path: Path
    agent_path: Path | None
    table: Mapping[str, EnvVar]


def resolve_targets(
    adapter: object, home: Path, env: Mapping[str, str]
) -> Targets:
    """Which files this agent's settings live in, and what it lets us set.

    Policy, not parsing, which is why it is here and not in the frontend:
    choosing the file an agent's env block lives in is the difference between
    configuring that agent and quietly corrupting another one's config. The
    frontend resolves the adapter by name and hands it over; everything after
    that is this module's decision.

    `env_settings` and `settings_path` are both *optional* capabilities,
    probed with getattr - see the rationale on AgentAdapter in agents/base.py.
    An adapter missing either one is reported as having no settable
    environment variables, which is the spec's documented outcome and is why
    the probe cannot simply be deleted as unreachable: claude-code is the only
    adapter in-tree that has them.
    """
    remem_path = Path(
        env.get("REMEM_CONFIG", remem_config.default_config_path())
    )
    table_of = getattr(adapter, "env_settings", None)
    path_of = getattr(adapter, "settings_path", None)
    if table_of is None or path_of is None:
        return Targets(remem_path, None, {})
    return Targets(remem_path, path_of(home, env), table_of())


def _agent_env(path: Path | None) -> dict:
    if path is None:
        return {}
    data, _ = jsonfile.read_json(path, set())
    block = data.get("env")
    return block if isinstance(block, dict) else {}


def list_settings(
    remem_path: Path,
    agent_path: Path | None,
    table: Mapping[str, EnvVar],
    env: Mapping[str, str],
) -> list[Setting]:
    """Every settable key, its effective value, and where that value came from.

    This is the feature that justifies the command existing alongside
    Claude Code's own /config: one view over both tools, with each side's
    precedence rule already applied.
    """
    rows: list[Setting] = []

    file_data = _read_toml(remem_path)

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
