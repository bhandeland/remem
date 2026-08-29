"""Reading and writing raw events, and the provenance beside them."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Event, EventKind, new_id
from remem.services import write
from remem.store import NotOwner

pytestmark = pytest.mark.db

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def an_event(owner, *, at=NOW, tool="Bash", session="s1", payload=None):
    return Event(
        id=new_id(),
        owner_id=owner.id,
        project="remem",
        harness="claude-code",
        session_id=session,
        kind=EventKind.TOOL_CALL,
        tool=tool,
        payload=payload if payload is not None else {"command": "ls"},
        occurred_at=at,
    )


def test_an_event_round_trips_with_its_payload_whole(store, owner):
    big = {"output": "x" * 50_000, "nested": {"a": [1, 2, 3]}}
    stored = store.put_event(an_event(owner, payload=big))

    got = store.events_for_session(owner.id, "remem", "claude-code", "s1")
    assert [e.id for e in got] == [stored.id]
    assert got[0].payload == big
    assert got[0].tool == "Bash"
    assert got[0].kind is EventKind.TOOL_CALL


def test_events_come_back_oldest_first(store, owner):
    later = store.put_event(an_event(owner, at=NOW + timedelta(minutes=5)))
    earlier = store.put_event(an_event(owner, at=NOW))

    got = store.events_for_session(owner.id, "remem", "claude-code", "s1")
    assert [e.id for e in got] == [earlier.id, later.id]


def test_since_excludes_events_at_or_before_the_watermark(store, owner):
    store.put_event(an_event(owner, at=NOW))
    after = store.put_event(an_event(owner, at=NOW + timedelta(minutes=5)))

    got = store.events_for_session(
        owner.id, "remem", "claude-code", "s1", since=NOW
    )
    assert [e.id for e in got] == [after.id]


def test_another_principals_events_are_invisible(store, owner):
    store.put_event(an_event(owner))
    other = store.ensure_principal("someone-else")
    assert store.events_for_session(
        other.id, "remem", "claude-code", "s1"
    ) == []


@pytest.mark.xfail(reason="prune_events lands in Task 7", strict=True)
def test_provenance_survives_the_events_it_names(store, owner):
    entry = write.remember(store, owner.id, title="t", body="b")
    event = store.put_event(an_event(owner))
    store.link_entry_events(entry.id, [event], owner.id)

    rows = store.provenance(entry.id, owner.id)
    assert rows == [(event.id, "s1", "claude-code", True)]

    store.prune_events(owner.id, before=NOW + timedelta(days=1), force=True)

    rows = store.provenance(entry.id, owner.id)
    assert rows == [(event.id, "s1", "claude-code", False)]


def test_provenance_for_another_owners_entry_raises(store, owner):
    entry = write.remember(store, owner.id, title="t", body="b")
    event = store.put_event(an_event(owner))
    other = store.ensure_principal("someone-else")
    with pytest.raises(NotOwner):
        store.link_entry_events(entry.id, [event], other.id)
