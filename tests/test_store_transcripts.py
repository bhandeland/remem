"""Store-level behaviour for transcripts. Every test here needs Postgres."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import Event, EventKind, Principal, TranscriptTrigger, new_id
from saddlebag.transcript_file import parse, sha256_hex
from tests.conftest import found

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


@pytest.fixture
def other(store: PostgresStore) -> Principal:
    return store.ensure_principal("someone-else")


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


def test_put_transcript_round_trips_content_byte_for_byte(
    store: PostgresStore, owner: Principal
) -> None:
    """The property the whole design rests on.

    Not an approximation of it: if the bytes come back different, the source
    row is worthless and every recovery path built on it is a lie.
    """
    content = b'{"type": "user"}\n' + b"\xff\xfe invalid utf-8\n"
    got = store.put_transcript(
        owner.id,
        "p",
        "claude-code",
        "s1",
        "/tmp/s1.jsonl",
        content,
        sha256_hex(content),
    )
    assert store.transcript_content(got.id, owner.id) == content


def test_put_transcript_is_idempotent_on_the_same_session(
    store: PostgresStore, owner: Principal
) -> None:
    """Re-importing a session updates it rather than creating a twin."""
    first = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/tmp/s1.jsonl", b"{}", "aaa"
    )
    second = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/tmp/s1.jsonl", b"{}{}", "bbb"
    )
    assert first.id == second.id
    assert second.bytes == 4
    assert second.sha256 == "bbb"


def test_append_transcript_adds_bytes_without_rewriting(
    store: PostgresStore, owner: Principal
) -> None:
    head = b'{"type": "user"}\n'
    tail = b'{"type": "assistant"}\n'
    t = store.put_transcript(
        owner.id,
        "p",
        "claude-code",
        "s1",
        "/tmp/s1.jsonl",
        head,
        sha256_hex(head),
    )
    assert (
        store.append_transcript(t.id, owner.id, tail, sha256_hex(head + tail)) is True
    )
    assert store.transcript_content(t.id, owner.id) == head + tail


def test_append_transcript_refuses_another_owner(
    store: PostgresStore, owner: Principal, other: Principal
) -> None:
    """Ownership is enforced inside the store, never by callers."""
    t = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/tmp/s1.jsonl", b"{}", "aaa"
    )
    assert store.append_transcript(t.id, other.id, b"{}", "bbb") is False


def test_replace_transcript_lines_rebuilds_from_scratch(
    store: PostgresStore, owner: Principal
) -> None:
    t = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/tmp/s1.jsonl", b"{}", "aaa"
    )
    lines, _ = parse(b'{"type": "user"}\n{"type": "assistant"}\n')
    assert store.replace_transcript_lines(t.id, lines) == 2
    assert store.transcript_line_count(t.id) == 2
    # Rebuilding with fewer lines must leave no orphans from the first pass.
    fewer, _ = parse(b'{"type": "user"}\n')
    assert store.replace_transcript_lines(t.id, fewer) == 1
    assert store.transcript_line_count(t.id) == 1


def test_add_transcript_lines_continues_the_sequence(
    store: PostgresStore, owner: Principal
) -> None:
    t = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/tmp/s1.jsonl", b"{}", "aaa"
    )
    head, _ = parse(b'{"type": "user"}\n')
    store.replace_transcript_lines(t.id, head)
    tail, _ = parse(b'{"type": "assistant"}\n', start_seq=1)
    assert store.add_transcript_lines(t.id, tail) == 1
    assert store.transcript_line_count(t.id) == 2


def test_stored_transcripts_lists_without_content(
    store: PostgresStore, owner: Principal
) -> None:
    store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/tmp/s1.jsonl", b"{}" * 100, "aaa"
    )
    got = store.stored_transcripts(owner.id, "p")
    assert len(got) == 1
    assert not hasattr(got[0], "content")


def test_add_transcript_path_names_the_project_already_holding_it(
    store: PostgresStore, owner: Principal
) -> None:
    """A directory belongs to at most one project, and the refusal says whose.

    Without the name, a user is told "taken" and has no way to find out by
    what - and the real failure only surfaces much later, as a session
    filed under the wrong project.
    """
    assert store.add_transcript_path(owner.id, "alpha", "/tmp/dir") is None
    assert store.add_transcript_path(owner.id, "beta", "/tmp/dir") == "alpha"


def test_add_transcript_path_is_idempotent_for_the_same_project(
    store: PostgresStore, owner: Principal
) -> None:
    assert store.add_transcript_path(owner.id, "alpha", "/tmp/dir") is None
    assert store.add_transcript_path(owner.id, "alpha", "/tmp/dir") is None
    assert len(store.transcript_paths(owner.id, "alpha")) == 1


def test_transcript_paths_with_no_project_sweeps_every_claim(
    store: PostgresStore, owner: Principal
) -> None:
    store.add_transcript_path(owner.id, "alpha", "/tmp/a")
    store.add_transcript_path(owner.id, "beta", "/tmp/b")
    assert len(store.transcript_paths(owner.id)) == 2


def test_a_started_run_has_no_finished_at(
    store: PostgresStore, owner: Principal
) -> None:
    """The started row is what makes 'crashed' distinguishable from 'never
    ran', and it only works if it commits before any file is read."""
    run = store.start_transcript_run(owner.id, "p", TranscriptTrigger.AUTO)
    assert run.finished_at is None
    assert found(store.latest_transcript_run(owner.id, "p")).finished_at is None


def test_finishing_a_run_records_its_counts(
    store: PostgresStore, owner: Principal
) -> None:
    run = store.start_transcript_run(owner.id, "p", TranscriptTrigger.MANUAL)
    done = store.finish_transcript_run(
        run.id,
        owner.id,
        files_seen=3,
        files_new=2,
        files_appended=1,
        files_rebuilt=0,
        lines_written=500,
        bytes_written=4096,
        anomalies=[{"path": "/tmp/x", "stored": 10, "on_disk": 4}],
        failures=[],
    )
    assert done.finished_at is not None
    assert done.files_new == 2
    assert done.anomalies[0]["on_disk"] == 4
    assert done.trigger is TranscriptTrigger.MANUAL


def test_event_session_ids_are_what_prove_a_directory_belongs_to_a_project(
    store: PostgresStore, owner: Principal
) -> None:
    """Discovery intersects these with filenames on disk.

    This is the whole answer to the rename problem: a transcript filename IS
    a session id, and events already record which project a session belongs
    to, so ownership is proven rather than guessed from a directory slug.
    """
    record_event_for(store, owner, project="p", session_id="session-1")
    assert "session-1" in store.event_session_ids(owner.id, "p")


def test_event_session_projects_names_the_project_each_session_was_recorded_under(
    store: PostgresStore, owner: Principal
) -> None:
    """`event_session_ids` cannot answer the import's question.

    That one takes the project as an argument, so it can only confirm what
    the caller already believes. The import needs the opposite: which
    project a session was recorded under, whatever the claiming project is.
    """
    record_event_for(store, owner, project="alpha", session_id="s1")
    record_event_for(store, owner, project="beta", session_id="s2")

    pairs = dict(store.event_session_projects(owner.id))

    assert pairs["s1"] == "alpha"
    assert pairs["s2"] == "beta"


def test_a_second_put_under_a_different_project_does_not_re_home_a_transcript(
    store: PostgresStore, owner: Principal
) -> None:
    """A transcript keeps the project it was first filed under.

    `project = excluded.project` in the upsert used to move it silently:
    every count on both sides is project-scoped, so one project's numbers
    dropped and the other's rose with nothing recorded anywhere. The import
    reports the disagreement as an anomaly instead - reported and stable,
    rather than moved and invisible. The rest of the row still updates,
    which is what makes this a deliberate exception and not an inert upsert.
    """
    body = b'{"type": "user"}\n'
    first = store.put_transcript(
        owner.id, "alpha", "claude-code", "s1", "/a/s1.jsonl", body, sha256_hex(body)
    )

    grown = body + b'{"type": "assistant"}\n'
    second = store.put_transcript(
        owner.id, "beta", "claude-code", "s1", "/b/s1.jsonl", grown, sha256_hex(grown)
    )

    assert second.id == first.id
    assert second.project == "alpha"
    assert second.path == "/b/s1.jsonl"
    assert second.bytes == len(grown)


def test_a_session_and_its_subagent_are_two_rows(
    store: PostgresStore, owner: Principal
) -> None:
    """A subagent file carries its parent's session id. Without `agent_id`
    in the identity, storing it would overwrite the parent's bytes."""
    parent = store.put_transcript(
        owner.id,
        "p",
        "claude-code",
        "s1",
        "/x/s1.jsonl",
        b"parent\n",
        sha256_hex(b"parent\n"),
    )
    child = store.put_transcript(
        owner.id,
        "p",
        "claude-code",
        "s1",
        "/x/s1/subagents/agent-a1.jsonl",
        b"child\n",
        sha256_hex(b"child\n"),
        agent_id="a1",
    )
    assert parent.id != child.id
    assert parent.agent_id is None
    assert child.agent_id == "a1"
    assert found(store.get_transcript(owner.id, "claude-code", "s1")).id == parent.id
    assert (
        found(store.get_transcript(owner.id, "claude-code", "s1", "a1")).id == child.id
    )
    assert store.transcript_content(parent.id, owner.id) == b"parent\n"


def test_a_subagent_put_twice_is_one_row(
    store: PostgresStore, owner: Principal
) -> None:
    first = store.put_transcript(
        owner.id,
        "p",
        "claude-code",
        "s1",
        "/x",
        b"a\n",
        sha256_hex(b"a\n"),
        agent_id="a1",
    )
    second = store.put_transcript(
        owner.id,
        "p",
        "claude-code",
        "s1",
        "/x",
        b"a\nb\n",
        sha256_hex(b"a\nb\n"),
        agent_id="a1",
    )
    assert first.id == second.id
    assert second.bytes == 4


def test_one_agent_id_under_two_sessions_is_two_rows(
    store: PostgresStore, owner: Principal
) -> None:
    """Real: four agent ids on this machine repeat across parents."""
    one = store.put_transcript(
        owner.id,
        "p",
        "claude-code",
        "s1",
        "/x",
        b"a\n",
        sha256_hex(b"a\n"),
        agent_id="a1",
    )
    two = store.put_transcript(
        owner.id,
        "p",
        "claude-code",
        "s2",
        "/y",
        b"b\n",
        sha256_hex(b"b\n"),
        agent_id="a1",
    )
    assert one.id != two.id


def test_a_second_session_row_with_no_agent_is_refused(
    store: PostgresStore, owner: Principal, conn: psycopg.Connection[Any]
) -> None:
    """`nulls not distinct` is load-bearing. Without it, two NULL-agent rows
    for one session are both admitted and the session's own transcript
    silently loses its uniqueness. Raw SQL, because `put_transcript` would
    upsert and never show the constraint at all."""
    store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/x", b"a\n", sha256_hex(b"a\n")
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "insert into transcripts"
            " (id, owner_id, project, harness, session_id, path,"
            "  content, bytes, sha256)"
            " values (%s, %s, 'p', 'claude-code', 's1', '/x', %s, 1, 'h')",
            (new_id(), owner.id, b"b"),
        )


def test_stored_transcripts_carry_the_agent(
    store: PostgresStore, owner: Principal
) -> None:
    store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/x", b"a\n", sha256_hex(b"a\n")
    )
    store.put_transcript(
        owner.id,
        "p",
        "claude-code",
        "s1",
        "/y",
        b"b\n",
        sha256_hex(b"b\n"),
        agent_id="a1",
    )
    got = {(t.session_id, t.agent_id) for t in store.stored_transcripts(owner.id, "p")}
    assert got == {("s1", None), ("s1", "a1")}
