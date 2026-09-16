from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import Principal
from saddlebag.services import transcripts
from saddlebag.transcript_file import sha256_hex

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


def test_designate_stores_an_absolute_path(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """Absolute, deliberately unlike reingest designate's repo-relative paths.

    These directories live outside any repository; there is no git root to
    resolve them against.
    """
    got = transcripts.designate(store, owner.id, "p", tmp_path)
    assert got == str(tmp_path.resolve())
    assert [c.path for c in store.transcript_paths(owner.id, "p")] == [got]


def test_designate_refuses_a_directory_that_does_not_exist(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """Loudly, because this is the one moment there is a human to tell."""
    with pytest.raises(transcripts.PathRefused, match="does not exist"):
        transcripts.designate(store, owner.id, "p", tmp_path / "nope")


def test_designate_refuses_a_file(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    target = tmp_path / "a.jsonl"
    target.write_text("{}")
    with pytest.raises(transcripts.PathRefused, match="not a directory"):
        transcripts.designate(store, owner.id, "p", target)


def test_designate_names_the_project_already_holding_the_directory(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    transcripts.designate(store, owner.id, "alpha", tmp_path)
    with pytest.raises(transcripts.PathRefused, match="alpha"):
        transcripts.designate(store, owner.id, "beta", tmp_path)


def test_designating_the_same_directory_twice_is_not_an_error(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.designate(store, owner.id, "p", tmp_path)
    assert len(store.transcript_paths(owner.id, "p")) == 1


def test_undesignate_drops_the_claim(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    transcripts.designate(store, owner.id, "p", tmp_path)
    assert transcripts.undesignate(store, owner.id, "p", tmp_path) is True
    assert store.transcript_paths(owner.id, "p") == []


def test_undesignate_leaves_imported_transcripts_alone(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """Dropping a claim stops future reading - it does not destroy sessions.

    Deleting stored transcripts is a separate, explicit act that does not
    exist yet, and the whole reason the source rows are kept byte-exact is
    that losing them is unrecoverable.
    """
    transcripts.designate(store, owner.id, "p", tmp_path)
    content = b'{"type": "user"}\n'
    store.put_transcript(
        owner.id,
        "p",
        transcripts.HARNESS,
        "s1",
        str(tmp_path / "s1.jsonl"),
        content,
        sha256_hex(content),
        agent_id=None,
    )

    transcripts.undesignate(store, owner.id, "p", tmp_path)

    survivors = store.stored_transcripts(owner.id, "p")
    assert [t.session_id for t in survivors] == ["s1"]
    assert store.transcript_content(survivors[0].id, owner.id) == content
