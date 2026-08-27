"""The pluggability seam. Third parties ship an adapter as a separate package
and register it under the 'remem.agents' entry point group."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Protocol


class UnsupportedScope(ValueError):
    """The requested install scope is not implemented by this adapter.

    Raised rather than quietly falling back: an install that reports success
    while having done something else is worse than one that refuses.
    """


@dataclass(slots=True)
class Identity:
    """Who wrote a memory, and in what context."""

    agent: str
    session_id: str | None = None
    project: str | None = None


@dataclass(slots=True)
class InstallReport:
    agent: str
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Things the user has to know to make the install do anything, printed
    #: after the actions. Not failures, so not warnings.
    notes: list[str] = field(default_factory=list)


class Kind(StrEnum):
    """How a setting's value is interpreted."""

    INT = "int"
    #: A real number. Separate from INT because the bound check differs: a
    #: float setting can need an *exclusive* lower bound (a similarity
    #: threshold of exactly 0 matches everything), which an integer range
    #: expresses by simply raising the minimum by one.
    FLOAT = "float"
    BOOL = "bool"
    #: Enabled by *presence*, whatever the value - so "0" enables it too.
    PRESENCE = "presence"
    STR = "str"


@dataclass(frozen=True, slots=True)
class EnvVar:
    """One settable environment variable, as an adapter declares it.

    Lives here rather than beside any particular table because
    services/settings.py validates against it, and a service that imported
    agents.claude_code would invert the pluggability seam.
    """

    name: str
    kind: Kind
    #: One line. Rendered by `remem config list`, so it must fit on a line.
    help: str
    #: Inclusive bounds, in the units the value is written in. Typed loosely
    #: because a Kind.FLOAT setting bounds a float and a Kind.INT one an int;
    #: the coerce branch for each kind is what gives them meaning.
    minimum: int | float | None = None
    maximum: int | float | None = None
    #: Makes `minimum` exclusive: the value must be strictly greater than it.
    #: Only meaningful for Kind.FLOAT - see the note on the member.
    exclusive_minimum: bool = False
    #: The documented default, shown when the key is unset. A string because
    #: that is what lands in settings.json and what the user typed.
    default: str | None = None
    #: Accepts 10m / 30s / 500ms as well as a raw millisecond integer. A
    #: six-digit millisecond literal invites an off-by-one-zero.
    duration: bool = False
    #: Surfaced by `list` for a variable that does not behave as it reads.
    note: str | None = None


class AgentAdapter(Protocol):
    name: str

    # env is injected rather than read from os.environ inside the adapter so
    # that tests can relocate an install without mutating the real process
    # environment - the same reason config.load() takes it.
    def install(
        self,
        scope: str,
        home: Path,
        env: Mapping[str, str] | None = None,
    ) -> InstallReport: ...
    def identity(self, env: Mapping[str, str], payload: dict) -> Identity: ...

    # Optional capability, probed with getattr rather than declared here:
    #
    #     def env_settings(self) -> Mapping[str, EnvVar]: ...
    #
    # Adapters written before `remem config` existed - including any third
    # party one already shipped - do not have it, and requiring it would break
    # them at import time. The registry contract is that a broken third-party
    # adapter warns rather than breaking remem, so the service probes instead.
