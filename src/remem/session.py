"""Opens a connection, guarantees migrations and the principal, hands back
everything a frontend needs. Frontends should never touch psycopg directly."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import psycopg
from psycopg import sql

from remem.backends.postgres.store import PostgresStore
from remem.config import Config, load
from remem.domain import Principal


@dataclass(slots=True)
class Session:
    conn: psycopg.Connection
    store: PostgresStore
    owner: Principal
    config: Config


def ensure_database(dsn: str) -> bool:
    """Create the target database if it does not exist. True if created."""
    parsed = urlparse(dsn)
    dbname = parsed.path.lstrip("/")
    admin_dsn = urlunparse(parsed._replace(path="/postgres"))
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        exists = admin.execute(
            "select 1 from pg_database where datname = %s", (dbname,)
        ).fetchone()
        if exists:
            return False
        # Postgres cannot parameterise an identifier, so quote it properly
        # rather than interpolating the raw string into the statement.
        admin.execute(
            sql.SQL("create database {}").format(sql.Identifier(dbname))
        )
        return True


@contextmanager
def open_session(config: Config | None = None, *, autocommit: bool = False):
    """Open a session. One transaction for the whole block by default.

    `autocommit=True` makes each statement durable on its own instead. That
    matters for long-running work that records its own progress: in a single
    transaction a genuinely failed statement leaves the connection in
    InFailedSqlTransaction, so every later statement - including the ones
    recording the failure - is silently swallowed and Postgres turns the final
    COMMIT into a ROLLBACK, discarding work that had already succeeded. It also
    avoids holding row locks open across minutes of subprocess work.
    """
    cfg = config or load()
    with psycopg.connect(cfg.dsn, autocommit=autocommit) as conn:
        store = PostgresStore(conn)
        owner = store.ensure_principal(cfg.user_handle)
        if not autocommit:
            conn.commit()
        yield Session(conn=conn, store=store, owner=owner, config=cfg)
        if not autocommit:
            conn.commit()
