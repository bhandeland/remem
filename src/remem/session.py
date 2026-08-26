"""Opens a connection, guarantees migrations and the principal, hands back
everything a frontend needs. Frontends should never touch psycopg directly."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import psycopg

from remem.backends.postgres.migrate import migrate
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
        admin.execute(f'create database "{dbname}"')
        return True


@contextmanager
def open_session(config: Config | None = None):
    cfg = config or load()
    with psycopg.connect(cfg.dsn) as conn:
        store = PostgresStore(conn)
        owner = store.ensure_principal(cfg.user_handle)
        conn.commit()
        yield Session(conn=conn, store=store, owner=owner, config=cfg)
        conn.commit()
