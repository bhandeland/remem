"""The live round-trip, for the harness it was written about.

claude-mem's opencode integration reported success for months while
recording nothing. This is the check that would have caught it.
"""

from __future__ import annotations

import psycopg
import pytest

from remem.agents.opencode.adapter import OpenCodeAdapter
from remem.agents.verify import VERIFY_PROJECT
from remem.backends.postgres.migrate import migrate

pytestmark = pytest.mark.db


@pytest.fixture
def env(live_dsn, tmp_path):
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    return {
        "REMEM_DSN": live_dsn,
        "REMEM_USER_ID": "brandon",
        "REMEM_CONFIG": str(tmp_path / "none.toml"),
    }


def test_verification_round_trips_a_real_event(env, tmp_path):
    report = OpenCodeAdapter().verify(env=env, home=tmp_path)

    assert any("round-trip" in a for a in report.actions)
    assert report.warnings == []


def test_verification_records_under_the_opencode_harness(env, tmp_path, monkeypatch):
    """Not 'claude-code'. The harness column is what `remem record status`
    groups by, so a shared round-trip that hardcoded a name would report
    the wrong harness as working.

    A hardcoded "claude-code" would still record, read back, and delete
    consistently - every other assertion in this file would pass regardless
    of which name the round-trip used. So this spies on the actual argument
    `record_service.record` is called with, rather than inferring the
    harness indirectly. The spy delegates to the real function; it does not
    replace the round-trip, only observes it.
    """
    from remem.services import record as record_service

    calls: list[str] = []
    real_record = record_service.record

    def spy(store, owner_id, harness_event, harness):
        calls.append(harness)
        return real_record(store, owner_id, harness_event, harness)

    monkeypatch.setattr(record_service, "record", spy)

    report = OpenCodeAdapter().verify(env=env, home=tmp_path)

    assert report.warnings == []
    assert calls == ["opencode"]


def test_verification_leaves_another_principal_untouched(env, live_dsn, tmp_path):
    """Pins two of the delete's four keys at once: owner and session.

    A scratch database holding nothing else is structurally incapable of
    catching an over-broad delete: absence of other rows is exactly what the
    fixture guarantees. So another principal's event has to be present, and
    has to be asserted surviving BY ID. Asserting the target is gone pins
    nothing.

    This is the shape of tests/test_events_prune.py's cross-owner test,
    adopted after install verification cleaned up with prune(force=True) and
    would have destroyed every user's entire event history.

    It does NOT, by itself, catch an owner-only (prune-style) delete: the
    seeded row belongs to a different owner, so an owner-scoped delete
    already excludes it for the wrong reason. That gap is what
    test_verification_leaves_the_same_owners_other_project_untouched below
    covers, by varying only one key - project - while keeping the owner the
    one the round-trip actually uses.
    """
    from datetime import datetime, timezone

    from remem.backends.postgres.store import PostgresStore
    from remem.domain import Event, EventKind, new_id

    with psycopg.connect(live_dsn) as c:
        other_store = PostgresStore(c)
        other = other_store.ensure_principal("someone-else")
        theirs = other_store.put_event(
            Event(
                id=new_id(),
                owner_id=other.id,
                project=VERIFY_PROJECT,
                harness="opencode",
                session_id="theirs",
                kind=EventKind.TOOL_CALL,
                tool="Bash",
                payload={"mine": False},
                occurred_at=datetime.now(timezone.utc),
            )
        )
        c.commit()

    report = OpenCodeAdapter().verify(env=env, home=tmp_path)
    assert report.warnings == []

    with psycopg.connect(live_dsn) as c:
        survived = PostgresStore(c).events_for_session(
            other.id, VERIFY_PROJECT, "opencode", "theirs"
        )
    assert [e.id for e in survived] == [theirs.id]


def test_verification_leaves_the_same_owners_other_project_untouched(
    env, live_dsn, tmp_path
):
    """Pins the one key the test above cannot: project, held constant across
    the same owner the round-trip actually uses.

    This is the case an owner-only delete - `services.events.prune`'s
    contract, and exactly the bug install verification used to have - would
    NOT be caught by the cross-owner test above, because that seeded row
    belongs to someone else and an owner-scoped delete already excludes it
    for the wrong reason. Here the seeded row shares the round-trip's own
    owner and harness, and differs only in project, so an owner-only delete
    removes it and this test fails.
    """
    from datetime import datetime, timezone

    from remem.backends.postgres.store import PostgresStore
    from remem.domain import Event, EventKind, new_id

    with psycopg.connect(live_dsn) as c:
        store = PostgresStore(c)
        # The same handle the round-trip's REMEM_USER_ID resolves to - not a
        # second principal. ensure_principal is idempotent by handle, so
        # this returns the identical id verify() will record under.
        owner = store.ensure_principal("brandon")
        mine_elsewhere = store.put_event(
            Event(
                id=new_id(),
                owner_id=owner.id,
                project="some-other-project",
                harness="opencode",
                session_id="not-the-verify-session",
                kind=EventKind.TOOL_CALL,
                tool="Bash",
                payload={"mine": True, "elsewhere": True},
                occurred_at=datetime.now(timezone.utc),
            )
        )
        c.commit()

    report = OpenCodeAdapter().verify(env=env, home=tmp_path)
    assert report.warnings == []

    with psycopg.connect(live_dsn) as c:
        survived = PostgresStore(c).events_for_session(
            owner.id, "some-other-project", "opencode", "not-the-verify-session"
        )
    assert [e.id for e in survived] == [mine_elsewhere.id]
