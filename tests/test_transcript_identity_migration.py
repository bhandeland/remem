"""Migration 024 over rows written before it.

The real database held 112 transcripts when 024 was written. The question
this answers is what those rows become: session transcripts (a NULL
`agent_id`), still unique per session, with room beside them for their
subagents.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest

from tests.test_vocabulary_migration import apply_rest, apply_through

pytestmark = pytest.mark.db

BEFORE = "023_transcript_runs"

_INSERT = (
    "insert into transcripts"
    " (id, owner_id, project, harness, session_id, path, content, bytes, sha256)"
    " values (%s, %s, 'p', 'claude-code', 's1', '/x', %s, 1, 'h')"
)


def test_rows_written_before_024_become_session_transcripts(
    conn: psycopg.Connection[Any],
) -> None:
    apply_through(conn, BEFORE)
    owner = uuid4()
    conn.execute(
        "insert into principals (id, handle, kind) values (%s, 'pre', 'user')",
        (owner,),
    )
    existing = uuid4()
    conn.execute(_INSERT, (existing, owner, b"a"))

    apply_rest(conn)

    row = conn.execute(
        "select agent_id from transcripts where id = %s", (existing,)
    ).fetchone()
    assert row is not None
    assert row[0] is None

    # Room for its subagent...
    conn.execute(
        "insert into transcripts"
        " (id, owner_id, project, harness, session_id, agent_id, path,"
        "  content, bytes, sha256)"
        " values (%s, %s, 'p', 'claude-code', 's1', 'a1', '/y', %s, 1, 'h')",
        (uuid4(), owner, b"b"),
    )
    # ...and none for a second copy of the session itself.
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(_INSERT, (uuid4(), owner, b"c"))
