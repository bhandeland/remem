"""The pluggability seam. Third parties ship an adapter as a separate package
and register it under the 'remem.agents' entry point group."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol


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


class AgentAdapter(Protocol):
    name: str

    def install(self, scope: str, home: Path) -> InstallReport: ...
    def identity(self, env: Mapping[str, str], payload: dict) -> Identity: ...
