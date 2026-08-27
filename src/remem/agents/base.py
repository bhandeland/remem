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
    minimum: int | None = None
    maximum: int | None = None
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
