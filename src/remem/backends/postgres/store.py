"""The Postgres Store implementation. Hand-written SQL, psycopg 3, no ORM."""

from __future__ import annotations

from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from remem.domain import (
    Entry,
    Kind,
    Origin,
    Principal,
    PrincipalKind,
    Scope,
    new_id,
)

ENTRY_FIELDS = [
    "id", "kind", "title", "body", "project", "scope", "owner_id", "tags",
    "links", "agent", "session_id", "origin", "superseded_by",
    "created_at", "updated_at",
]


def entry_columns(alias: str = "") -> str:
    """Column list, optionally table-qualified for joins."""
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{f}" for f in ENTRY_FIELDS)


def _row_to_entry(row: dict) -> Entry:
    return Entry(
        id=row["id"],
        kind=Kind(row["kind"]),
        title=row["title"],
        body=row["body"],
        owner_id=row["owner_id"],
        project=row["project"],
        scope=Scope(row["scope"]),
        tags=list(row["tags"] or []),
        links=[UUID(str(x)) for x in (row["links"] or [])],
        agent=row["agent"],
        session_id=row["session_id"],
        origin=Origin(row["origin"]),
        superseded_by=row["superseded_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PostgresStore:
    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def _cur(self):
        return self._conn.cursor(row_factory=dict_row)

    # ---------------- principals ----------------

    def ensure_principal(self, handle: str) -> Principal:
        with self._cur() as cur:
            cur.execute(
                """
                insert into principals (id, handle) values (%s, %s)
                on conflict (handle) do update set handle = excluded.handle
                returning id, handle, display_name, kind, created_at
                """,
                (new_id(), handle),
            )
            row = cur.fetchone()
        return Principal(
            id=row["id"],
            handle=row["handle"],
            display_name=row["display_name"],
            kind=PrincipalKind(row["kind"]),
            created_at=row["created_at"],
        )

    def get_principal(self, handle: str) -> Principal | None:
        with self._cur() as cur:
            cur.execute(
                "select id, handle, display_name, kind, created_at "
                "from principals where handle = %s",
                (handle,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Principal(
            id=row["id"],
            handle=row["handle"],
            display_name=row["display_name"],
            kind=PrincipalKind(row["kind"]),
            created_at=row["created_at"],
        )

    # ---------------- entries ----------------

    def put_entry(self, entry: Entry) -> Entry:
        with self._cur() as cur:
            cur.execute(
                f"""
                insert into entries (
                  id, kind, title, body, project, scope, owner_id, tags, links,
                  agent, session_id, origin, superseded_by
                ) values (
                  %(id)s, %(kind)s, %(title)s, %(body)s, %(project)s, %(scope)s,
                  %(owner_id)s, %(tags)s, %(links)s, %(agent)s, %(session_id)s,
                  %(origin)s, %(superseded_by)s
                )
                on conflict (id) do update set
                  kind = excluded.kind, title = excluded.title,
                  body = excluded.body, project = excluded.project,
                  scope = excluded.scope, tags = excluded.tags,
                  links = excluded.links, agent = excluded.agent,
                  session_id = excluded.session_id, origin = excluded.origin,
                  superseded_by = excluded.superseded_by,
                  updated_at = clock_timestamp()
                returning {entry_columns()}
                """,
                {
                    "id": entry.id,
                    "kind": str(entry.kind),
                    "title": entry.title,
                    "body": entry.body,
                    "project": entry.project,
                    "scope": str(entry.scope),
                    "owner_id": entry.owner_id,
                    "tags": list(entry.tags),
                    "links": [str(x) for x in entry.links],
                    "agent": entry.agent,
                    "session_id": entry.session_id,
                    "origin": str(entry.origin),
                    "superseded_by": entry.superseded_by,
                },
            )
            return _row_to_entry(cur.fetchone())

    def get_entry(self, entry_id: UUID, owner_id: UUID) -> Entry | None:
        with self._cur() as cur:
            cur.execute(
                f"select {entry_columns()} from entries "
                "where id = %s and owner_id = %s",
                (entry_id, owner_id),
            )
            row = cur.fetchone()
        return _row_to_entry(row) if row else None

    def set_superseded(self, old_id: UUID, new_entry_id: UUID, owner_id: UUID) -> bool:
        with self._cur() as cur:
            cur.execute(
                "update entries set superseded_by = %s, updated_at = clock_timestamp() "
                "where id = %s and owner_id = %s",
                (new_entry_id, old_id, owner_id),
            )
            return cur.rowcount == 1
