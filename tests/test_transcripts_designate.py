from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import Principal
from saddlebag.services import transcripts

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


def test_designate_stores_an_absolute_path(store, owner, tmp_path: Path) -> None:
    """Absolute, deliberately unlike reingest designate's repo-relative paths.

    These directories live outside any repository; there is no git root to
    resolve them against.
    """
    got = transcripts.designate(store, owner.id, "p", tmp_path)
    assert got == str(tmp_path.resolve())
    assert [c.path for c in store.transcript_paths(owner.id, "p")] == [got]


def test_designate_refuses_a_directory_that_does_not_exist(
    store, owner, tmp_path: Path
) -> None:
    """Loudly, because this is the one moment there is a human to tell."""
    with pytest.raises(transcripts.PathRefused, match="does not exist"):
        transcripts.designate(store, owner.id, "p", tmp_path / "nope")


def test_designate_refuses_a_file(store, owner, tmp_path: Path) -> None:
    target = tmp_path / "a.jsonl"
    target.write_text("{}")
    with pytest.raises(transcripts.PathRefused, match="not a directory"):
        transcripts.designate(store, owner.id, "p", target)


def test_designate_names_the_project_already_holding_the_directory(
    store, owner, tmp_path: Path
) -> None:
    transcripts.designate(store, owner.id, "alpha", tmp_path)
    with pytest.raises(transcripts.PathRefused, match="alpha"):
        transcripts.designate(store, owner.id, "beta", tmp_path)


def test_designating_the_same_directory_twice_is_not_an_error(
    store, owner, tmp_path: Path
) -> None:
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.designate(store, owner.id, "p", tmp_path)
    assert len(store.transcript_paths(owner.id, "p")) == 1
