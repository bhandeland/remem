"""The portability seam. One implementation today (Postgres); a file backend
would implement this same protocol."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from remem.domain import (
    Collection,
    Entry,
    Event,
    ExtractJob,
    HarnessStats,
    Hit,
    JobStatus,
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
    #: Per-harness recording health for `remem record status`. `idle_seconds`
    #: and `max_attempts` are threaded through rather than read from config
    #: here so this stays in step with `sessions_awaiting_extraction` and
    #: extraction's own give-up rule - see the Postgres implementation.
    def event_stats(
        self, owner_id: UUID, idle_seconds: int, max_attempts: int
    ) -> list[HarnessStats]: ...

    # events
    def put_event(self, event: Event) -> Event: ...
    def events_for_session(
        self, owner_id: UUID, project: str, harness: str, session_id: str,
        since: datetime | None = None, limit: int = 500,
    ) -> list[Event]: ...
    def link_entry_events(
        self, entry_id: UUID, events: list[Event], owner_id: UUID
    ) -> None: ...
    def provenance(
        self, entry_id: UUID, owner_id: UUID
    ) -> list[tuple[UUID, str, str, bool]]: ...
    def prune_events(
        self, owner_id: UUID, before: datetime, force: bool
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
