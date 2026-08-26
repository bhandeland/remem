"""Database fixtures.

A scratch database is created once per session and dropped at the end. Each
test runs inside a transaction that is rolled back, so tests are isolated
without paying for schema setup every time.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

ADMIN_DSN = os.environ.get(
    "REMEM_TEST_DSN", "postgresql://remem:remem@localhost:5433/remem"
)

SKIP_REASON = (
    "Postgres is not reachable at %s. Start it with `docker compose up -d` "
    "(and make sure Docker itself is running)." % ADMIN_DSN
)


def _server_is_up() -> bool:
    try:
        with psycopg.connect(ADMIN_DSN, connect_timeout=2):
            return True
    except psycopg.Error:
        return False


@pytest.fixture(scope="session")
def db_dsn():
    if not _server_is_up():
        pytest.skip(SKIP_REASON)

    name = f"remem_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'create database "{name}"')
    try:
        yield ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity "
                "where datname = %s",
                (name,),
            )
            admin.execute(f'drop database if exists "{name}"')


@pytest.fixture
def conn(db_dsn):
    """A connection whose work is rolled back when the test ends."""
    with psycopg.connect(db_dsn) as c:
        yield c
        c.rollback()


@pytest.fixture(scope="session")
def live_dsn():
    """A SECOND scratch database, for tests that must commit.

    The CLI, MCP, and hook tests open their own connections through
    open_session(), so their setup has to be committed to be visible. If they
    shared `db_dsn`, a committed migration would make the migration tests
    ("nothing pending") pass or fail depending on test order.
    """
    if not _server_is_up():
        pytest.skip(SKIP_REASON)

    name = f"remem_live_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'create database "{name}"')
    try:
        yield ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity "
                "where datname = %s",
                (name,),
            )
            admin.execute(f'drop database if exists "{name}"')
