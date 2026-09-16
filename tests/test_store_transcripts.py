"""Store-level behaviour for transcripts. Every test here needs Postgres."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import Principal
from saddlebag.transcript_file import parse, sha256_hex

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


def test_put_transcript_round_trips_content_byte_for_byte(store, owner) -> None:
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


def test_put_transcript_is_idempotent_on_the_same_session(store, owner) -> None:
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


def test_append_transcript_adds_bytes_without_rewriting(store, owner) -> None:
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


def test_append_transcript_refuses_another_owner(store, owner, other) -> None:
    """Ownership is enforced inside the store, never by callers."""
    t = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/tmp/s1.jsonl", b"{}", "aaa"
    )
    assert store.append_transcript(t.id, other.id, b"{}", "bbb") is False


def test_replace_transcript_lines_rebuilds_from_scratch(store, owner) -> None:
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


def test_add_transcript_lines_continues_the_sequence(store, owner) -> None:
    t = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/tmp/s1.jsonl", b"{}", "aaa"
    )
    head, _ = parse(b'{"type": "user"}\n')
    store.replace_transcript_lines(t.id, head)
    tail, _ = parse(b'{"type": "assistant"}\n', start_seq=1)
    assert store.add_transcript_lines(t.id, tail) == 1
    assert store.transcript_line_count(t.id) == 2


def test_stored_transcripts_lists_without_content(store, owner) -> None:
    store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/tmp/s1.jsonl", b"{}" * 100, "aaa"
    )
    got = store.stored_transcripts(owner.id, "p")
    assert len(got) == 1
    assert not hasattr(got[0], "content")
