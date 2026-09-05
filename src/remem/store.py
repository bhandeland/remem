"""The portability seam. One implementation today (Postgres); a file backend
would implement this same protocol."""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from remem.domain import (
    Collection,
    Entry,
    Event,
    ExtractJob,
    DuplicateGroup,
    DuplicateSet,
    HarnessStats,
    Hit,
    IngestDesignation,
    IngestRun,
    IngestTrigger,
    JobStatus,
    MemoryDesignation,
    NearPair,
    Principal,
    Query,
    SessionRef,
)


class NotOwner(PermissionError):
    """A write targeted a row that belongs to a different principal.

    Ownership is enforced in the store, not left to callers: a backend that
    silently rewrote another principal's row would be an integrity hole no
    service-layer check could close.
    """


class Store(Protocol):
    # principals
    def ensure_principal(self, handle: str) -> Principal: ...
    def get_principal(self, handle: str) -> Principal | None: ...

    # entries
    def put_entry(self, entry: Entry) -> Entry: ...
    def get_entry(self, entry_id: UUID, owner_id: UUID) -> Entry | None: ...
    def set_superseded(self, old_id: UUID, new_entry_id: UUID, owner_id: UUID) -> bool: ...
    def search(self, query: Query, owner_id: UUID) -> list[Hit]: ...
    def fuzzy_search(
        self, query: Query, owner_id: UUID, threshold: float
    ) -> list[Hit]: ...

    # vectors
    def exact_duplicate_groups(
        self, query: Query, owner_id: UUID
    ) -> list[DuplicateSet]: ...

    def near_duplicate_pairs(
        self, query: Query, owner_id: UUID, model: str,
        threshold: float, limit: int,
    ) -> tuple[list[NearPair], int]: ...

    def vector_coverage(
        self, query: Query, owner_id: UUID, model: str
    ) -> tuple[int, int]: ...

    def put_vector(
        self, entry_id: UUID, model: str, dim: int,
        vector: list[float], owner_id: UUID,
    ) -> None: ...
    def entries_missing_vectors(
        self, owner_id: UUID, model: str, limit: int
    ) -> list[Entry]: ...
    def semantic_search(
        self, query: Query, owner_id: UUID, vector: list[float],
        model: str, threshold: float,
    ) -> list[Hit]: ...

    # collections
    def put_collection(self, collection: Collection) -> Collection: ...
    def get_collection(self, slug: str, owner_id: UUID) -> Collection | None: ...
    def list_collections(self, owner_id: UUID) -> list[Collection]: ...
    def pin(
        self, collection_id: UUID, entry_id: UUID, position: int, owner_id: UUID
    ) -> None: ...
    def pinned_entries(self, collection_id: UUID, owner_id: UUID) -> list[Entry]: ...

    # recording
    def set_record_enabled(self, owner_id: UUID, project: str, enabled: bool) -> None: ...
    def record_enabled(self, owner_id: UUID, project: str) -> bool: ...
    def enabled_record_projects(self, owner_id: UUID) -> list[str]: ...
    #: The retired capture spool, counted once so it is visible rather than
    #: mysterious. See 010_retire_capture_jobs.sql.
    def pending_legacy_capture_jobs(self, owner_id: UUID) -> int: ...
    #: Per-harness recent volume for `remem record status`. Deliberately
    #: silent on backlog: `sessions_awaiting` comes back 0 here always, and
    #: `services.events.status` fills it in via `services.extraction`'s
    #: "given up" rule rather than the store re-deriving that policy.
    def event_stats(self, owner_id: UUID) -> list[HarnessStats]: ...
    #: Events with no harness id of their own that repeat within one
    #: session - the duplicate 011's unique index cannot reach. Read-only
    #: and advisory: nothing deletes on the strength of it.
    def duplicate_unkeyed_events(
        self, owner_id: UUID, limit: int = 20
    ) -> list[DuplicateGroup]: ...

    # memory export
    def set_memory_collection(
        self, owner_id: UUID, project: str, slug: str | None,
        working_dir: str | None = None,
    ) -> None: ...
    def memory_collection(self, owner_id: UUID, project: str) -> str | None: ...
    #: Every designation this owner has, for `sync --all`. Returns the
    #: recorded working directory too, which is the only thing that makes
    #: syncing a project other than the current one possible at all.
    def memory_designations(self, owner_id: UUID) -> list[MemoryDesignation]: ...

    # ingest designations
    #: `paths=None` clears the (project, archive) designation. Passing a
    #: list replaces it wholesale - a designation is the whole set, never
    #: something appended to.
    def set_ingest_paths(
        self, owner_id: UUID, project: str, paths: list[str] | None,
        archive: bool = False,
    ) -> None: ...
    #: Every designation for one project, or for every project when
    #: `project` is None - the latter is what a future `refresh --all`
    #: would read, and what `remem ingest status` lists today.
    def ingest_designations(
        self, owner_id: UUID, project: str | None = None
    ) -> list[IngestDesignation]: ...

    # ingest runs
    #: Opens a row and returns it. Called before any file is read, so that a
    #: process which dies mid-run leaves a started, unfinished row behind.
    def start_ingest_run(
        self, owner_id: UUID, project: str, trigger: IngestTrigger,
        archive: bool = False,
    ) -> IngestRun: ...
    #: Records the outcome. Raises NotOwner for a row that is not the
    #: caller's - ownership is enforced here, not by callers.
    def finish_ingest_run(
        self, run_id: UUID, owner_id: UUID, *,
        created: int, changed: int, unchanged: int, swept: int, embedded: int,
        failures: list[dict], twins: list[dict], embed_error: str | None,
    ) -> None: ...
    #: The newest-started row for one project, finished or not.
    def latest_ingest_run(
        self, owner_id: UUID, project: str
    ) -> IngestRun | None: ...

    #: Live ingested/archived entries in a project that carry a `src:` tag
    #: and no `sec:` tag - one per ingested document. `search` cannot say
    #: "has a tag with this prefix and lacks one with that prefix", and
    #: pulling every chunk through it to filter in Python meets Query.limit
    #: on any project with a few hundred chunks. Newest first.
    def anchors(self, owner_id: UUID, project: str) -> list[Entry]: ...

    # events
    def put_event(self, event: Event) -> Event: ...
    def events_for_session(
        self, owner_id: UUID, project: str, harness: str, session_id: str,
        since: datetime | None = None, limit: int = 500,
    ) -> list[Event]: ...
    #: Deletes every event for one (owner, project, harness, session) -
    #: nothing wider. Exists for install verification's cleanup, which must
    #: not be able to touch anything outside the reserved project it wrote
    #: to even in principle; `prune_events` is an operator command whose
    #: contract is a time window over everything an owner has, and reaching
    #: for it to delete one known event was reaching for the wrong tool.
    def delete_session_events(
        self, owner_id: UUID, project: str, harness: str, session_id: str
    ) -> int: ...
    def link_entry_events(
        self, entry_id: UUID, events: list[Event], owner_id: UUID
    ) -> None: ...
    def provenance(
        self, entry_id: UUID, owner_id: UUID
    ) -> list[tuple[UUID, str, str, bool]]: ...
    #: `project` narrows the prune to one project; None means all of them.
    #: The gate that decides whether an event is ever recorded is per
    #: project, so the one that deletes it has to be too.
    def prune_events(
        self, owner_id: UUID, before: datetime, force: bool,
        project: str | None = None,
    ) -> tuple[int, int, int]: ...

    # extraction spool
    def sessions_awaiting_extraction(
        self, owner_id: UUID, idle_seconds: int, limit: int
    ) -> list[SessionRef]: ...
    def extract_job_for_session(
        self, owner_id: UUID, session: SessionRef
    ) -> ExtractJob | None: ...
    def claim_extract_job(self, owner_id: UUID, session: SessionRef) -> ExtractJob: ...
    def claim_extract_job_by_id(
        self, job_id: UUID, owner_id: UUID
    ) -> ExtractJob | None: ...
    def finish_extract_job(
        self, job_id: UUID, owner_id: UUID, status: JobStatus,
        error: str | None, entries_written: int,
        covers_through: datetime | None,
    ) -> None: ...
    def get_extract_job(self, job_id: UUID, owner_id: UUID) -> ExtractJob | None: ...
    def extract_job_counts(self, owner_id: UUID) -> dict[str, int]: ...
    def recent_failed_extract_jobs(
        self, owner_id: UUID, limit: int = 5
    ) -> list[ExtractJob]: ...
    def try_advisory_lock(self, name: str, owner_id: UUID) -> bool: ...

    def transaction(self) -> AbstractContextManager[None]:
        """Group statements that must commit or roll back together.

        Under an autocommit connection this opens a real transaction; inside
        an already-open transaction (the common case in tests, which run
        inside one rolled-back transaction per test) it is a savepoint -
        psycopg's `Connection.transaction()` nests either way. It exists
        because `write.supersede` is two statements - insert the
        replacement, then retire the old row - and `reingest run` runs
        autocommit so its run row survives a crash. Without this, a process
        killed between those two statements leaves two live entries sharing
        the same `(src:, sec:)` pair, and no later run can sweep the loser:
        it is not in `ingest_file`'s `existing` dict (keyed on slug, one
        winner per slug) so it is never superseded and never swept.
        """
        ...
