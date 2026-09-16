from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import Event, EventKind, Principal, TranscriptTrigger, new_id
from saddlebag.services import transcripts
from tests.transcript_tree import write_session, write_subagent

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


def record_event_for(
    store: PostgresStore, owner: Principal, *, project: str, session_id: str
) -> None:
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


def test_status_of_an_unclaimed_project_is_a_full_document(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """Same keys in every state, so a consumer checks a field for null
    rather than branching on which keys arrived."""
    got = transcripts.status_to_dict(transcripts.status(store, owner.id, "p", tmp_path))
    assert set(got) == {"project", "paths", "run", "backlog", "irrecoverable"}
    assert got["run"] is None
    assert got["paths"] == []


def test_status_reports_a_missing_claimed_directory_as_missing(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """Never as clean. A directory that has gone is a definite statement."""
    claimed = tmp_path / "gone"
    claimed.mkdir()
    transcripts.designate(store, owner.id, "p", claimed)
    claimed.rmdir()
    got = transcripts.status(store, owner.id, "p", tmp_path)
    assert got.paths[0].present is False


def test_status_counts_the_backlog(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    for i in range(3):
        (tmp_path / f"s{i}.jsonl").write_bytes(b'{"type": "user"}\n')
    transcripts.designate(store, owner.id, "p", tmp_path)
    assert transcripts.status(store, owner.id, "p", tmp_path).backlog == 3


def test_status_counts_sessions_whose_transcript_is_gone(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """This number only grows, and it is the argument for importing sooner
    stated as a measurement rather than as urgency."""
    record_event_for(store, owner, project="p", session_id="vanished")
    got = transcripts.status(store, owner.id, "p", tmp_path)
    assert got.irrecoverable == 1


def test_advisories_name_a_run_that_did_not_finish(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    transcripts.designate(store, owner.id, "p", tmp_path)
    store.start_transcript_run(owner.id, "p", TranscriptTrigger.AUTO)
    lines = transcripts.advisories(store, owner.id)
    assert any("did not finish" in line for line in lines)


def test_advisories_say_nothing_about_backlog_alone(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """A refresh is bounded by design, so a nonzero backlog is the normal
    state between runs. A line that fires every time is ignored."""
    (tmp_path / "s1.jsonl").write_bytes(b'{"type": "user"}\n')
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL, cap=0)
    assert transcripts.advisories(store, owner.id) == []


def test_advisories_sweep_every_claimed_project(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """Unlike ingest's status, this can answer for every project: a claim
    stores an absolute path and needs no recorded working directory."""
    for name in ("alpha", "beta"):
        directory = tmp_path / name
        directory.mkdir()
        transcripts.designate(store, owner.id, name, directory)
        directory.rmdir()
    lines = transcripts.advisories(store, owner.id)
    assert len(lines) == 2


def test_advisories_count_a_conflicting_session_once(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """One session with many subagents is one thing to go and look at.
    Per-file entries stay in the run row; the line counts sessions."""
    record_event_for(store, owner, project="B", session_id="s1")
    write_session(tmp_path, "s1")
    for agent in ("a1", "a2", "a3"):
        write_subagent(tmp_path, "s1", agent)
    transcripts.designate(store, owner.id, "A", tmp_path)
    transcripts.run(store, owner.id, "A", trigger=TranscriptTrigger.MANUAL)

    lines = transcripts.advisories(store, owner.id)

    assert len(lines) == 1
    assert "1 session(s) recorded under another project" in lines[0]
