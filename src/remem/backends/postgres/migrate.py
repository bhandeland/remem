"""Numbered .sql files applied in order, tracked in schema_migrations."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import psycopg

_TRACKING_TABLE = """
create table if not exists schema_migrations (
  version text primary key,
  applied_at timestamptz not null default clock_timestamp()
)
"""


def migration_files() -> list[tuple[str, str]]:
    """(version, sql) pairs sorted by filename.

    Public because a migration whose job is to rewrite existing data can
    only be tested by standing in the middle of the sequence - applying up
    to the version before it, writing rows the old way, and then applying
    the rest.
    """
    package = resources.files("remem.backends.postgres") / "migrations"
    out = []
    for item in sorted(package.iterdir(), key=lambda p: p.name):
        if item.name.endswith(".sql"):
            out.append((Path(item.name).stem, item.read_text()))
    return out


def applied_versions(conn: psycopg.Connection) -> list[str]:
    """Versions already applied. Read-only: inspecting a database must not
    write to it, so an unmigrated database reports [] rather than having the
    tracking table created as a side effect of asking."""
    exists = conn.execute(
        "select to_regclass('public.schema_migrations')"
    ).fetchone()[0]
    if exists is None:
        return []
    rows = conn.execute("select version from schema_migrations order by version")
    return [r[0] for r in rows.fetchall()]


def pending_versions(conn: psycopg.Connection) -> list[str]:
    done = set(applied_versions(conn))
    return [v for v, _ in migration_files() if v not in done]


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply every pending migration. Returns the versions newly applied.

    The CALLER owns commit and rollback. This runs the whole batch inside the
    caller's transaction, so an exception partway through leaves nothing
    applied provided the caller rolls back (or simply does not commit). A
    caller that swallows the exception and commits anyway would leave
    schema_migrations disagreeing with the schema.
    """
    conn.execute(_TRACKING_TABLE)
    done = set(applied_versions(conn))
    newly = []
    for version, sql in migration_files():
        if version in done:
            continue
        conn.execute(sql)
        conn.execute(
            "insert into schema_migrations (version) values (%s)", (version,)
        )
        newly.append(version)
    return newly
