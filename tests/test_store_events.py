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

    got = store.events_for_session(owner.id, "remem", "claude-code", "s1", since=NOW)
    assert [e.id for e in got] == [after.id]


def test_another_principals_events_are_invisible(store, owner):
    store.put_event(an_event(owner))
    other = store.ensure_principal("someone-else")
    assert store.events_for_session(other.id, "remem", "claude-code", "s1") == []


def test_delete_session_events_is_scoped_to_all_four_keys(store, owner):
    """This is install verification's cleanup primitive - it exists because
    `prune_events` deletes by owner and a time window, and reaching for that
    to clean up one known event would take every other event this owner has
    recorded with it. Every dimension it does not name must survive."""
    target = store.put_event(an_event(owner, session="s1"))
    other_session = store.put_event(an_event(owner, session="s2"))
    other_project = store.put_event(
        Event(
            id=new_id(),
            owner_id=owner.id,
            project="other-project",
            harness="claude-code",
            session_id="s1",
            kind=EventKind.TOOL_CALL,
            tool="Bash",
            payload={},
            occurred_at=NOW,
        )
    )
    other_harness = store.put_event(
        Event(
            id=new_id(),
            owner_id=owner.id,
            project="remem",
            harness="cursor",
            session_id="s1",
            kind=EventKind.TOOL_CALL,
            tool="Bash",
            payload={},
            occurred_at=NOW,
        )
    )

    deleted = store.delete_session_events(owner.id, "remem", "claude-code", "s1")

    assert deleted == 1
    assert store.events_for_session(owner.id, "remem", "claude-code", "s1") == []
    survivors = {
        e.id
        for e in (
            store.events_for_session(owner.id, "remem", "claude-code", "s2")
            + store.events_for_session(owner.id, "other-project", "claude-code", "s1")
            + store.events_for_session(owner.id, "remem", "cursor", "s1")
        )
    }
    assert survivors == {other_session.id, other_project.id, other_harness.id}


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


# --- Deduplication by the harness's own event id (migration 011) ---------
#
# Recording is one INSERT from a fail-soft hook, so the same event reaching
# the store twice is a real possibility: a hook registered twice (which
# happened - see the claude-code and cursor install repairs), a harness
# retrying, a `remem events process` racing a live session.
#
# The key is the harness's OWN id, not remem's. Nothing remem computes can
# tell a duplicate from a genuine repeat: `Bash: git status` twice in a
# session is ordinary, and every payload carries a duration that differs
# between two recordings of the same event, so payload equality both misses
# real duplicates and risks dropping legitimate ones.


def a_cursor_event(owner, *, hook, gen, kind=EventKind.MESSAGE, tool_use_id=None):
    payload = {"hook_event_name": hook, "generation_id": gen}
    if tool_use_id is not None:
        payload["tool_use_id"] = tool_use_id
    return Event(
        id=new_id(),
        owner_id=owner.id,
        project="remem",
        harness="cursor",
        session_id="s1",
        kind=kind,
        tool=None,
        payload=payload,
        occurred_at=NOW,
    )


def test_the_same_tool_use_id_is_recorded_once(store, owner):
    """The duplicate a twice-registered hook produces.

    Both recordings carry the harness's id for one tool call. The second is
    dropped rather than raising: the caller is a fail-soft hook, and an
    exception there is how an install problem becomes a broken session.
    """
    first = store.put_event(an_event(owner, payload={"tool_use_id": "tu_1"}))
    store.put_event(an_event(owner, payload={"tool_use_id": "tu_1", "duration_ms": 9}))

    got = store.events_for_session(owner.id, "remem", "claude-code", "s1")
    assert [e.id for e in got] == [first.id]
    assert got[0].payload == {"tool_use_id": "tu_1"}, "first write wins"


def test_a_repeated_tool_call_with_no_harness_id_is_still_recorded_twice(store, owner):
    """The legitimate repeat that payload-equality dedup would have eaten.

    Running the same command twice in a session is ordinary. With no id in
    the payload there is nothing to dedupe on, and inventing one would lose
    a real event - so both are kept, exactly as before 011.
    """
    store.put_event(an_event(owner, payload={"command": "git status"}))
    store.put_event(an_event(owner, payload={"command": "git status"}))

    got = store.events_for_session(owner.id, "remem", "claude-code", "s1")
    assert len(got) == 2


def test_a_prompt_and_its_response_share_a_generation_id_and_both_survive(store, owner):
    """The bug this key was one edit away from shipping.

    generation_id identifies a GENERATION, not an event: cursor's
    beforeSubmitPrompt and afterAgentResponse both carry the same one, and
    so does every tool call in between. Keying on it alone would have
    silently discarded every agent response cursor ever recorded - three
    such collisions were already sitting in the live database when this was
    written. The hook name is what separates them.
    """
    prompt = store.put_event(a_cursor_event(owner, hook="beforeSubmitPrompt", gen="g1"))
    response = store.put_event(
        a_cursor_event(owner, hook="afterAgentResponse", gen="g1")
    )

    got = store.events_for_session(owner.id, "remem", "cursor", "s1")
    assert {e.id for e in got} == {prompt.id, response.id}


def test_the_same_generation_and_hook_is_recorded_once(store, owner):
    """A duplicated cursor message hook still dedupes."""
    first = store.put_event(a_cursor_event(owner, hook="afterAgentResponse", gen="g1"))
    store.put_event(a_cursor_event(owner, hook="afterAgentResponse", gen="g1"))

    got = store.events_for_session(owner.id, "remem", "cursor", "s1")
    assert [e.id for e in got] == [first.id]


def test_tool_use_id_wins_over_the_generation_it_belongs_to(store, owner):
    """Two tool calls in one generation share generation_id and differ only
    by tool_use_id - so the tool id has to be preferred, not appended to."""
    a = store.put_event(
        a_cursor_event(
            owner,
            hook="postToolUse",
            gen="g1",
            kind=EventKind.TOOL_CALL,
            tool_use_id="tu_a",
        )
    )
    b = store.put_event(
        a_cursor_event(
            owner,
            hook="postToolUse",
            gen="g1",
            kind=EventKind.TOOL_CALL,
            tool_use_id="tu_b",
        )
    )

    got = store.events_for_session(owner.id, "remem", "cursor", "s1")
    assert {e.id for e in got} == {a.id, b.id}


def test_the_same_id_in_two_sessions_is_two_events(store, owner):
    """The key is scoped to the session, not global. Harness ids are only
    promised unique within their own session, and a collision across two
    should never cost an event."""
    store.put_event(an_event(owner, session="s1", payload={"tool_use_id": "tu_1"}))
    store.put_event(an_event(owner, session="s2", payload={"tool_use_id": "tu_1"}))

    assert len(store.events_for_session(owner.id, "remem", "claude-code", "s1")) == 1
    assert len(store.events_for_session(owner.id, "remem", "claude-code", "s2")) == 1


def test_a_dropped_duplicate_still_returns_a_usable_event(store, owner):
    """put_event returns the stored event either way.

    It reads recorded_at off the INSERT, and `on conflict do nothing`
    returns no row - so a naive implementation raises a TypeError inside a
    hook that is supposed to be incapable of failing.
    """
    store.put_event(an_event(owner, payload={"tool_use_id": "tu_1"}))
    again = store.put_event(an_event(owner, payload={"tool_use_id": "tu_1"}))

    assert again.recorded_at is not None
