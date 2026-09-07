"""Install verification: the live round-trip that makes a silently broken
install loud at the one moment the user is watching.

claude-mem's opencode integration reported success for months while
recording nothing. `remem record status` (Task 8) makes that visible on
demand; this file is what makes it visible at install time, without waiting
for anyone to ask.
"""

from __future__ import annotations

import psycopg
import pytest

from remem.agents.claude_code.adapter import VERIFY_PROJECT, ClaudeCodeAdapter
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


def test_install_verification_round_trips_a_real_event(env, tmp_path):
    """An install that cannot demonstrate recording says so.

    One live round-trip - record, read back, delete - is what makes that
    failure loud at the only moment the user is watching.
    """
    report = ClaudeCodeAdapter().verify(env=env, home=tmp_path)

    assert any("round-trip" in a for a in report.actions)
    assert report.warnings == []


def test_install_verification_cleans_up_after_itself(env, tmp_path):
    """Nothing is left behind: not the test event, not the setting."""
    ClaudeCodeAdapter().verify(env=env, home=tmp_path)

    with psycopg.connect(env["REMEM_DSN"]) as c:
        assert (
            c.execute(
                "select count(*) from events where project = %s", (VERIFY_PROJECT,)
            ).fetchone()[0]
            == 0
        )
        enabled = c.execute(
            "select enabled from record_settings where project = %s",
            (VERIFY_PROJECT,),
        ).fetchone()
        assert enabled is None or enabled[0] is False


def test_install_verification_does_not_touch_events_outside_the_verify_project(
    env, tmp_path
):
    """The bug this test exists to catch: cleanup scoped only to
    (owner_id, before) rather than to the reserved project, harness, and
    session would delete every event this owner has ever recorded, not just
    the one verification wrote. A second, unrelated, older event for the
    same owner in a different project must survive the round-trip."""
    from datetime import datetime, timedelta, timezone

    from remem.backends.postgres.store import PostgresStore
    from remem.domain import Event, EventKind, new_id

    with psycopg.connect(env["REMEM_DSN"]) as c:
        store = PostgresStore(c)
        owner = store.ensure_principal("brandon")
        other = store.put_event(
            Event(
                id=new_id(),
                owner_id=owner.id,
                project="some-real-project",
                harness="claude-code",
                session_id="s-real",
                kind=EventKind.TOOL_CALL,
                tool="Bash",
                payload={"command": "ls"},
                occurred_at=datetime.now(timezone.utc) - timedelta(days=1),
            )
        )
        c.commit()

    ClaudeCodeAdapter().verify(env=env, home=tmp_path)

    with psycopg.connect(env["REMEM_DSN"]) as c:
        row = c.execute("select id from events where id = %s", (other.id,)).fetchone()
        assert row is not None, (
            "verify()'s cleanup deleted an event outside the reserved "
            "verification project"
        )


def test_install_verification_reports_a_failure_rather_than_raising():
    """An unreachable database becomes a warning naming what could not be
    demonstrated - never an exception, and the caller still finishes."""
    report = ClaudeCodeAdapter().verify(
        env={"REMEM_DSN": "postgresql://nobody@127.0.0.1:1/none"}
    )

    assert report.warnings != []
    assert any("verify" in w.lower() for w in report.warnings)
    assert report.actions == []


def test_the_hook_table_names_every_hook_the_install_registers():
    """The table install() reads and the table hook_state() reads are one
    table. Literals, not the constant: a guard that agrees with a wrong
    table is how PostToolUse went missing for the life of the pipeline."""
    from remem.agents.claude_code.adapter import HOOK_ENTRIES

    assert [h.event for h in HOOK_ENTRIES] == [
        "SessionStart",
        "SessionEnd",
        "PostToolUse",
        "UserPromptSubmit",
    ]


def test_only_the_hooks_that_lose_events_are_required():
    """SessionEnd loses no events, only the promptness of the idle timer.
    UserPromptSubmit is the handoff warning, not the record path."""
    from remem.agents.claude_code.adapter import HOOK_ENTRIES

    required = {h.event for h in HOOK_ENTRIES if h.required}
    assert required == {"SessionStart", "PostToolUse"}


def test_every_expected_hook_says_what_is_lost_without_it():
    from remem.agents.claude_code.adapter import HOOK_ENTRIES

    assert all(h.provides.strip() for h in HOOK_ENTRIES)
