"""Domain types. Pure data - no I/O, no SQL, no formatting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid7


class Kind(StrEnum):
    NOTE = "note"
    DOC = "doc"
    RULE = "rule"


class Scope(StrEnum):
    PERSONAL = "personal"
    TEAM = "team"


class Origin(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    #: Written by the extractor from a session's events. Was 'capture'.
    EXTRACTED = "extracted"
    HANDOFF = "handoff"
    #: A chunk of a markdown document loaded by `remem ingest`. In
    #: DEFAULT_ORIGINS: this is the reasoning layer, and it is what ingest
    #: exists to make searchable.
    INGESTED = "ingested"
    #: An ingested chunk held out of default results - implementation plans,
    #: whose text is mostly source code that now lives in src/. Not in
    #: DEFAULT_ORIGINS; reachable with --archived.
    ARCHIVED = "archived"


class Match(StrEnum):
    """How a hit matched, and therefore how much to trust it.

    Search runs three tiers and never blends them, so exactly one of these
    describes every hit in a result set. One field rather than an
    accumulating set of booleans: `fuzzy` alone could not distinguish a
    semantic match from a trigram one, and those deserve different trust.
    """

    EXACT = "exact"
    SEMANTIC = "semantic"
    FUZZY = "fuzzy"


class JobStatus(StrEnum):
    """The extract spool's status, and extract_jobs' own type (see
    migrations/009_extract_jobs.sql): reusing capture_status would tie an
    enum the retired table still carries to a migration that has nothing to
    do with it."""

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
    #: The one-line description a Claude Code memory file carries in its
    #: frontmatter. Nullable because every other origin has no such thing.
    summary: str | None = None
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


@dataclass(slots=True, frozen=True)
class MemoryDesignation:
    """A project's memory export: which collection, and from where.

    `working_dir` is the directory the designation was made from, and it is
    here because the two halves are keyed on different things - the
    designation on the project, Claude Code's memory directory on the
    absolute working directory. A worktree and its main checkout share a
    project and have two separate memory directories, so neither key
    derives the other and the answer has to be recorded rather than
    computed. None for a designation made before it was recorded: an
    honest gap, not a default.
    """

    project: str
    collection: str
    working_dir: str | None = None


@dataclass(slots=True, frozen=True)
class IngestDesignation:
    """A project's re-ingest set: which paths, and whether they are archive.

    `archive` is part of the identity rather than a field alongside the
    paths, because a project's specs and its plans are two separate
    invocations with different origins - the table holds at most two rows
    per project, one for each.

    Paths are repo-relative and resolved against the git root at refresh
    time. Storing them absolute would tie a designation to the machine that
    made it; ingest already resolves its project from the git common dir,
    so the root is always derivable where it is needed.
    """

    project: str
    paths: tuple[str, ...]
    archive: bool = False


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

    `match` says which tier answered. Callers must be able to tell the
    difference: an agent handed an approximate match with no marker would
    cite it as certain.
    """

    entry: Entry
    rank: float
    snippet: str
    match: Match = Match.EXACT


@dataclass(slots=True)
class ExtractJob:
    """One session queued for extraction from its events.

    Keyed on (owner_id, project, harness, session_id), not a transcript path
    - see 009_extract_jobs.sql. `covers_through` is the watermark: set on
    finish, it is the newest occurred_at among the events this run actually
    read, and is what lets a resumed session be re-queued for only its new
    events instead of being invisible forever.
    """

    id: UUID
    owner_id: UUID
    project: str
    harness: str
    session_id: str
    covers_through: datetime | None = None
    status: JobStatus = JobStatus.PENDING
    attempts: int = 0
    error: str | None = None
    entries_written: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EventKind(StrEnum):
    """What a harness handed us. Small and closed on purpose.

    Everything harness-specific lives in the payload, unparsed: a harness
    that changes its payload shape must not be able to break the write path.
    A fourth value is deliberately deferred until something writes one -
    adding an enum value later is cheap, and guessing now invites a label
    nothing ever produces.
    """

    TOOL_CALL = "tool_call"
    MESSAGE = "message"
    SESSION_END = "session_end"


@dataclass(slots=True)
class Event:
    """One thing that happened, as raw as it reached us."""

    id: UUID
    owner_id: UUID
    project: str
    harness: str
    session_id: str
    kind: EventKind
    payload: dict
    tool: str | None = None
    occurred_at: datetime | None = None
    recorded_at: datetime | None = None


@dataclass(slots=True)
class HarnessStats:
    """Per-harness recording health, as `remem record status` reports it.

    Only a harness that has recorded at least one event can appear here -
    there is no name to key a zero row on for one that never has. That is
    exactly the failure `record status` exists to catch (an adapter bound to
    hook names its harness never emits records nothing and looks like a
    quiet day), so `services.events.render` turns an empty list of these
    into a visible "no events" line rather than an absent section.
    """

    harness: str
    events_24h: int
    last_event_at: datetime | None
    sessions_awaiting: int


@dataclass(slots=True)
class DuplicateGroup:
    """One suspected duplicate, as `remem record status` reports it.

    Only events with no harness id of their own are ever counted here -
    011's unique index already makes a duplicate impossible for the rest.
    This is a report and never a delete, which is what makes payload
    equality an acceptable signal: a false positive costs a line of output.
    """

    project: str
    harness: str
    session_id: str
    count: int


@dataclass(slots=True)
class ProvenanceRow:
    """One event behind an entry, as `remem events show` reports it.

    `present` is what lets the forensic lookup tell "we recorded where this
    came from and then deleted the raw" apart from "we never recorded
    anything" - see `Store.provenance`, the left join this is built from.
    """

    event_id: UUID
    session_id: str
    harness: str
    present: bool


@dataclass(slots=True)
class SessionRef:
    """A session with events, as the idle trigger sees it.

    `extract_from` is the watermark: the newest `covers_through` of a done
    extract job for this session, or None when nothing has ever extracted
    it. Events at or before it have already produced whatever they were
    going to produce.
    """

    project: str
    harness: str
    session_id: str
    event_count: int
    last_event_at: datetime
    extract_from: datetime | None = None
