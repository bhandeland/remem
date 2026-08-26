"""Numbered .sql files applied in order, tracked in schema_migrations."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import psycopg

_TRACKING_TABLE = """
create table if not exists schema_migrations (
  version text primary key,
  applied_at timestamptz not null default now()
)
"""


def _migration_files() -> list[tuple[str, str]]:
    """(version, sql) pairs sorted by filename."""
    package = resources.files("remem.backends.postgres") / "migrations"
    out = []
    for item in sorted(package.iterdir(), key=lambda p: p.name):
        if item.name.endswith(".sql"):
            out.append((Path(item.name).stem, item.read_text()))
    return out


def applied_versions(conn: psycopg.Connection) -> list[str]:
    conn.execute(_TRACKING_TABLE)
    rows = conn.execute("select version from schema_migrations order by version")
    return [r[0] for r in rows.fetchall()]


def pending_versions(conn: psycopg.Connection) -> list[str]:
    done = set(applied_versions(conn))
    return [v for v, _ in _migration_files() if v not in done]


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply every pending migration. Returns the versions newly applied."""
    done = set(applied_versions(conn))
    newly = []
    for version, sql in _migration_files():
        if version in done:
            continue
        conn.execute(sql)
        conn.execute(
            "insert into schema_migrations (version) values (%s)", (version,)
        )
        newly.append(version)
    return newly
