"""Discovery proposes; it never writes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import Event, EventKind, Principal, new_id
from saddlebag.services import transcripts

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


def _transcript(directory: Path, session_id: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{session_id}.jsonl").write_bytes(
        json.dumps({"type": "user"}).encode() + b"\n"
    )


def record_event_for(store, owner: Principal, *, project: str, session_id: str) -> None:
    """One recorded event for a session, which is all discovery needs.

    Discovery proves a directory belongs to a project by intersecting
    recorded session ids with transcript filenames, so the only field that
    matters here is `session_id`. Everything else is the shape `Event`
    requires.
    """
    store.put_event(
        Event(
            id=new_id(),
            owner_id=owner.id,
            project=project,
            harness="claude-code",
            session_id=session_id,
            kind=EventKind.TOOL_CALL,
            tool="Bash",
            payload={"command": "ls"},
            occurred_at=datetime(2026, 9, 15, tzinfo=UTC),
        )
    )


def test_discover_proves_ownership_by_session_id_not_by_directory_name(
    store, owner, tmp_path: Path
) -> None:
    """The answer to the rename problem.

    A directory named for the OLD binary holds this project's sessions, and
    discovery finds it because transcript filenames are session ids and
    events already record which project each session belongs to.
    """
    old = tmp_path / "-Users-b-llmworkspace-oldname"
    _transcript(old, "sess-1")
    _transcript(old, "sess-2")
    _transcript(old, "never-recorded")
    record_event_for(store, owner, project="p", session_id="sess-1")
    record_event_for(store, owner, project="p", session_id="sess-2")

    found = transcripts.discover(store, owner.id, "p", tmp_path)

    assert len(found) == 1
    assert found[0].path == str(old)
    assert found[0].matched == 2
    assert found[0].total == 3


def test_discover_writes_nothing(store, owner, tmp_path: Path) -> None:
    """Widening scope is always a human act. This command only proposes."""
    old = tmp_path / "-dir"
    _transcript(old, "sess-1")
    record_event_for(store, owner, project="p", session_id="sess-1")
    transcripts.discover(store, owner.id, "p", tmp_path)
    assert store.transcript_paths(owner.id, "p") == []


def test_discover_marks_a_directory_already_claimed(
    store, owner, tmp_path: Path
) -> None:
    old = tmp_path / "-dir"
    _transcript(old, "sess-1")
    record_event_for(store, owner, project="p", session_id="sess-1")
    store.add_transcript_path(owner.id, "p", str(old))
    found = transcripts.discover(store, owner.id, "p", tmp_path)
    assert found[0].claimed_by == "p"


def test_discover_cannot_see_a_directory_with_no_recorded_sessions(
    store, owner, tmp_path: Path
) -> None:
    """The stated floor, not an oversight.

    A worktree directory holding 9 transcripts and zero recorded sessions is
    invisible here and claimable only by a human who knows it exists. That is
    exactly why discovery is a proposal rather than an algorithm.
    """
    _transcript(tmp_path / "-worktree", "unrecorded-1")
    assert transcripts.discover(store, owner.id, "p", tmp_path) == []
