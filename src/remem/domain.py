"""Domain types. Pure data - no I/O, no SQL, no formatting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid7


class Kind(StrEnum):
    MEMORY = "memory"
    DOC = "doc"
    RULE = "rule"


class Scope(StrEnum):
    PERSONAL = "personal"
    TEAM = "team"


class Origin(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    CAPTURE = "capture"
    HANDOFF = "handoff"


class CaptureStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class PrincipalKind(StrEnum):
    USER = "user"
    TEAM = "team"


def new_id() -> UUID:
    """Time-sortable id. uuid7 is stdlib on Python 3.14."""
    return uuid7()


@dataclass(slots=True)
class Principal:
    id: UUID
    handle: str
    display_name: str | None = None
    kind: PrincipalKind = PrincipalKind.USER
    created_at: datetime | None = None


@dataclass(slots=True)
class Entry:
    id: UUID
    kind: Kind
    title: str
    body: str
    owner_id: UUID
    project: str | None = None
    scope: Scope = Scope.PERSONAL
    tags: list[str] = field(default_factory=list)
    links: list[UUID] = field(default_factory=list)
    agent: str | None = None
    session_id: str | None = None
    origin: Origin = Origin.AGENT
    superseded_by: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class CollectionQuery:
    """The 'smart' half of a collection's membership."""

    tags: list[str] = field(default_factory=list)
    kinds: list[Kind] = field(default_factory=list)
    project: str | None = None

    def is_empty(self) -> bool:
        return not self.tags and not self.kinds and self.project is None

    def to_dict(self) -> dict:
        return {
            "tags": list(self.tags),
            "kinds": [str(k) for k in self.kinds],
            "project": self.project,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> CollectionQuery:
        data = data or {}
        return cls(
            tags=list(data.get("tags") or []),
            kinds=[Kind(k) for k in (data.get("kinds") or [])],
            project=data.get("project"),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CollectionQuery):
            return NotImplemented
        return self.to_dict() == other.to_dict()


@dataclass(slots=True)
class Collection:
    id: UUID
    slug: str
    title: str
    owner_id: UUID
    description: str | None = None
    project: str | None = None
    scope: Scope = Scope.PERSONAL
    query: CollectionQuery = field(default_factory=CollectionQuery)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class Query:
    """A search request. Owner filtering is applied by the store, not here."""

    text: str | None = None
    kinds: list[Kind] = field(default_factory=list)
    project: str | None = None
    tags: list[str] = field(default_factory=list)
    since: datetime | None = None
    include_superseded: bool = False
    origins: list[Origin] = field(default_factory=list)
    """Include only these origins. Empty means all origins."""
    limit: int = 20


@dataclass(slots=True)
class Hit:
    """A search result.

    `fuzzy` is True when this came from typo-tolerant fallback rather than an
    exact match. Callers must be able to tell the difference: an agent handed
    an approximate match with no marker would cite it as certain.
    """

    entry: Entry
    rank: float
    snippet: str
    fuzzy: bool = False


@dataclass(slots=True)
class CaptureJob:
    """One session queued for distillation."""

    id: UUID
    owner_id: UUID
    project: str
    transcript_path: str
    session_id: str | None = None
    status: CaptureStatus = CaptureStatus.PENDING
    attempts: int = 0
    error: str | None = None
    entries_written: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
