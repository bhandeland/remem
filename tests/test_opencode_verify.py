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


def test_verification_records_under_the_opencode_harness(env, tmp_path):
    """Not 'claude-code'. The harness column is what `remem record status`
    groups by, so a shared round-trip that hardcoded a name would report
    the wrong harness as working."""
    from remem.config import load
    from remem.session import open_session

    report = OpenCodeAdapter().verify(env=env, home=tmp_path)

    assert report.warnings == []
    with open_session(load(env=env)) as s:
        assert s.store.enabled_record_projects(s.owner.id) == []


def test_verification_leaves_another_principal_untouched(env, live_dsn, tmp_path):
    """The cross-owner assertion, and the reason this file exists at all.

    A scratch database holding nothing else is structurally incapable of
    catching an over-broad delete: absence of other rows is exactly what the
    fixture guarantees. So another principal's event has to be present, and
    has to be asserted surviving BY ID. Asserting the target is gone pins
    nothing.

    This is the shape of tests/test_events_prune.py's cross-owner test,
    adopted after install verification cleaned up with prune(force=True) and
    would have destroyed every user's entire event history.
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
