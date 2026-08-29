"""The opt-in gate, and the single INSERT behind it.

The gate is checked HERE and nowhere else. Every frontend - the hook, the
CLI, and whatever a third-party adapter does - gets it for free, and the one
place to look when asking "could this project have recorded anything" is
this module.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from remem.agents.base import HarnessEvent
from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import CaptureStatus, EventKind
from remem.services import capture, record

pytestmark = pytest.mark.db

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


@pytest.fixture
def transcript(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"role":"user","content":"hello"}\n')
    return str(p)


def a_harness_event(**kw):
    return HarnessEvent(
        kind=kw.get("kind", EventKind.TOOL_CALL),
        session_id=kw.get("session_id", "s1"),
        project=kw.get("project", "remem"),
        tool=kw.get("tool", "Bash"),
        payload=kw.get("payload", {"command": "ls"}),
        occurred_at=kw.get("occurred_at", NOW),
    )


def test_nothing_is_recorded_for_a_project_that_did_not_opt_in(store, owner):
    assert record.record(store, owner.id, a_harness_event(), "claude-code") is None
    assert store.events_for_session(
        owner.id, "remem", "claude-code", "s1"
    ) == []


def test_an_opted_in_project_records(store, owner):
    record.enable(store, owner.id, "remem")

    event = record.record(store, owner.id, a_harness_event(), "claude-code")

    assert event is not None
    stored = store.events_for_session(owner.id, "remem", "claude-code", "s1")
    assert [e.id for e in stored] == [event.id]
    assert stored[0].harness == "claude-code"


def test_disable_closes_the_gate_again(store, owner):
    record.enable(store, owner.id, "remem")
    record.disable(store, owner.id, "remem")
    assert record.record(store, owner.id, a_harness_event(), "claude-code") is None


def test_an_event_with_no_project_is_refused(store, owner):
    """A project-less event cannot be gated, so it must not be recorded.

    The opt-in is per project. An event that does not know which project it
    belongs to would have to be either recorded unconditionally - defeating
    the gate - or attributed to a guess. Refusing is the only honest answer.
    """
    record.enable(store, owner.id, "remem")
    assert record.record(
        store, owner.id, a_harness_event(project=None), "claude-code"
    ) is None


def test_the_opt_in_is_per_project_not_global(store, owner):
    record.enable(store, owner.id, "remem")
    assert record.record(
        store, owner.id, a_harness_event(project="other"), "claude-code"
    ) is None


# Moved from test_capture_service.py: these exercise the same opt-in gate
# through capture.enqueue, which checks it exactly as record.record does.
# capture.enable/disable are unchanged aliases of record.enable/disable.


def test_enqueue_returns_none_when_capture_is_disabled(store, owner, transcript):
    assert capture.enqueue(store, owner.id, project="remem",
                           transcript_path=transcript, session_id="s") is None


def test_enqueue_creates_a_job_when_enabled(store, owner, transcript):
    capture.enable(store, owner.id, "remem")
    job = capture.enqueue(store, owner.id, project="remem",
                          transcript_path=transcript, session_id="s")
    assert job is not None
    assert job.status is CaptureStatus.PENDING


def test_disable_stops_enqueueing(store, owner, transcript):
    capture.enable(store, owner.id, "remem")
    capture.disable(store, owner.id, "remem")
    assert capture.enqueue(store, owner.id, project="remem",
                           transcript_path=transcript, session_id="s") is None
