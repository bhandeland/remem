"""`remem record status` and `remem events show` - making recording's
silence visible, and telling "pruned" apart from "never recorded".

Fail-soft hooks make "recording nothing, silently, forever" the default
failure mode for this pipeline. These two commands are the on-demand answer:
a harness that has recorded nothing must show up as a zero, not be missing
from the report, and an event that was deleted after being used must not
look like one that was never recorded at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.cli import app
from remem.domain import Event, EventKind, JobStatus, SessionRef, new_id
from remem.services import events, extraction, record, write

runner = CliRunner()

pytestmark = pytest.mark.db

NOW = datetime.now(timezone.utc)
IDLE = 1200


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def an_event(owner, *, at=NOW, harness="claude-code", session="s1", tool="Bash"):
    return Event(
        id=new_id(),
        owner_id=owner.id,
        project="remem",
        harness=harness,
        session_id=session,
        kind=EventKind.TOOL_CALL,
        tool=tool,
        payload={"command": "ls"},
        occurred_at=at,
    )


def _mark_done(store, owner, session_id, covers_through, harness="claude-code"):
    """Give a session a done extract job with the given watermark, the same
    way `process` would after actually extracting it."""
    job = store.claim_extract_job(
        owner.id,
        SessionRef(
            project="remem", harness=harness, session_id=session_id,
            event_count=0, last_event_at=covers_through,
        ),
    )
    store.finish_extract_job(
        job.id, owner.id, JobStatus.DONE, None, 0, covers_through
    )


def test_status_reports_a_harness_that_has_recorded_nothing(store, owner):
    """The failure this whole command exists for.

    An adapter wired to hook names its harness never emits records nothing
    and reports success while doing it. The only way that becomes visible is
    a number the user can look at, so a harness with no events must appear
    in the report - as a zero - rather than being absent from it.
    """
    record.enable(store, owner.id, "remem")

    report = events.status(store, owner.id, idle_seconds=IDLE)

    assert report.enabled_projects == ["remem"]
    assert report.harnesses == []
    assert "no events" in events.render(report)


def test_status_counts_events_in_the_last_day_per_harness(store, owner):
    """One event 2h old, one 40h old: only the recent one counts toward
    events_24h, and last_event_at names the newer of the two."""
    older = store.put_event(an_event(owner, at=NOW - timedelta(hours=40)))
    newer = store.put_event(an_event(owner, at=NOW - timedelta(hours=2)))

    report = events.status(store, owner.id, idle_seconds=IDLE)

    assert len(report.harnesses) == 1
    stats = report.harnesses[0]
    assert stats.harness == "claude-code"
    assert stats.events_24h == 1
    assert stats.last_event_at == newer.occurred_at
    assert stats.last_event_at != older.occurred_at


def test_status_names_sessions_still_awaiting_extraction(store, owner):
    """A quiet session with no done extract job is outstanding work, and it
    is counted against its harness."""
    store.put_event(
        an_event(owner, at=NOW - timedelta(hours=2), session="s1")
    )

    report = events.status(store, owner.id, idle_seconds=IDLE)

    assert len(report.harnesses) == 1
    assert report.harnesses[0].sessions_awaiting == 1

    # Once the session has a done job covering its only event, there is
    # nothing left outstanding.
    _mark_done(store, owner, "s1", covers_through=NOW - timedelta(hours=2))
    report = events.status(store, owner.id, idle_seconds=IDLE)
    assert report.harnesses[0].sessions_awaiting == 0


class _AlwaysFails:
    """An extractor that always raises, to drive a job to its attempt cap."""

    def extract(self, events, project, known_titles=None):
        raise RuntimeError("claude exploded")


def test_status_excludes_a_session_whose_extraction_has_given_up(store, owner):
    """`sessions_awaiting_extraction` computes watermarks from DONE jobs
    only, so a session whose job FAILED past the attempt cap still has
    outstanding events by that definition alone. `record status` must not
    report that as work outstanding - it is a permanently stuck session
    `events process` already skips, and counting it would misreport the
    backlog. This is the same "given up" rule `extraction._gave_up` applies
    to `process`, now shared via `extraction.awaiting_sessions` rather than
    re-derived - see the comment on `services.events.status`."""
    store.put_event(an_event(owner, at=NOW - timedelta(hours=2), session="s1"))
    boom = _AlwaysFails()

    for _ in range(extraction.MAX_ATTEMPTS + 1):
        extraction.process(store, owner.id, boom, idle_seconds=IDLE, limit=10)

    report = events.status(store, owner.id, idle_seconds=IDLE)

    assert report.harnesses[0].sessions_awaiting == 0


def test_status_mentions_a_stranded_legacy_capture_job_once(live_dsn, monkeypatch, tmp_path):
    """The dead spool must be visible rather than mysterious."""
    import psycopg

    with psycopg.connect(live_dsn) as conn:
        migrate(conn)
        live_owner = PostgresStore(conn).ensure_principal("brandon")
        conn.execute(
            "insert into capture_jobs_legacy (id, owner_id, project, "
            "transcript_path) values (%s, %s, %s, %s)",
            (new_id(), live_owner.id, "remem", "/tmp/t.jsonl"),
        )
        conn.commit()

    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))

    result = runner.invoke(app, ["record", "status"])

    assert result.exit_code == 0
    assert result.stdout.count("capture_jobs_legacy") == 1


def test_events_show_says_pruned_rather_than_not_found(store, owner):
    """"We recorded where this came from and then deleted the raw" and "we
    never recorded anything" are different answers, and a user who cannot
    tell them apart concludes provenance was never recorded at all."""
    entry = write.remember(store, owner.id, title="t", body="b")
    event = store.put_event(an_event(owner, at=NOW - timedelta(days=40)))
    store.link_entry_events(entry.id, [event], owner.id)
    _mark_done(store, owner, "s1", covers_through=event.occurred_at)

    report = events.prune(store, owner.id, before=NOW)
    assert report.deleted == 1

    rows = events.forensics(store, owner.id, entry.id)
    rendered = events.render_provenance(rows)

    assert "event pruned" in rendered
    assert "not found" not in rendered


def test_events_show_on_an_entry_with_no_provenance(store, owner):
    """A hand-written entry has no events and never will. That is an
    ordinary answer, not an error."""
    entry = write.remember(store, owner.id, title="t", body="b")

    rows = events.forensics(store, owner.id, entry.id)
    rendered = events.render_provenance(rows)

    assert rows == []
    assert "not found" not in rendered
    assert "error" not in rendered.lower()
