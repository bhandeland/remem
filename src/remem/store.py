"""The portability seam. One implementation today (Postgres); a file backend
would implement this same protocol."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from remem.domain import Collection, Entry, Hit, Principal, Query


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

    # collections
    def put_collection(self, collection: Collection) -> Collection: ...
    def get_collection(self, slug: str, owner_id: UUID) -> Collection | None: ...
    def list_collections(self, owner_id: UUID) -> list[Collection]: ...
    def pin(
        self, collection_id: UUID, entry_id: UUID, position: int, owner_id: UUID
    ) -> None: ...
    def pinned_entries(self, collection_id: UUID, owner_id: UUID) -> list[Entry]: ...
