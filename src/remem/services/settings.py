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
    if raw.isdigit():
        return int(raw)
    match = _DURATION.match(raw.strip().lower())
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
