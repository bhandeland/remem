"""Migration 025 over rows written before it.

The real database held 356 transcripts and their run history when 025 was
written. Every one of them must read back as "no sidecar stored" - that
NULL is what the import's re-read rule keys on to backfill them.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest

from tests.test_vocabulary_migration import apply_rest, apply_through

pytestmark = pytest.mark.db

BEFORE = "024_transcript_agent_id"


def test_rows_written_before_025_have_no_sidecar(
    conn: psycopg.Connection[Any],
) -> None:
    apply_through(conn, BEFORE)
    owner = uuid4()
    conn.execute(
        "insert into principals (id, handle, kind) values (%s, 'pre', 'user')",
        (owner,),
    )
    existing = uuid4()
    conn.execute(
        "insert into transcripts"
        " (id, owner_id, project, harness, session_id, agent_id, path,"
        "  content, bytes, sha256)"
        " values (%s, %s, 'p', 'claude-code', 's1', 'a1', '/y', %s, 1, 'h')",
        (existing, owner, b"b"),
    )
    run = uuid4()
    conn.execute(
        "insert into transcript_runs (id, owner_id, project, trigger)"
        " values (%s, %s, 'p', 'auto')",
        (run, owner),
    )

    apply_rest(conn)

    row = conn.execute(
        "select meta from transcripts where id = %s", (existing,)
    ).fetchone()
    assert row is not None
    assert row[0] is None
    counted = conn.execute(
        "select metas_written from transcript_runs where id = %s", (run,)
    ).fetchone()
    assert counted is not None
    assert counted[0] == 0
