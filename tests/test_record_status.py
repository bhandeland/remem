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
from remem.domain import Event, EventKind, IngestTrigger, JobStatus, SessionRef, new_id
from remem.services import events, extraction, ingest, record, write

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
            project="remem",
            harness=harness,
            session_id=session_id,
            event_count=0,
            last_event_at=covers_through,
        ),
    )
    store.finish_extract_job(job.id, owner.id, JobStatus.DONE, None, 0, covers_through)


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
    store.put_event(an_event(owner, at=NOW - timedelta(hours=2), session="s1"))

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


def test_status_mentions_a_stranded_legacy_capture_job_once(
    live_dsn, monkeypatch, tmp_path
):
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
    """ "We recorded where this came from and then deleted the raw" and "we
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


# ---------------- the duplicate detector ----------------
#
# 011 made a duplicate impossible for any event carrying a harness id. It
# cannot cover the rest: claude-code's SessionEnd payload has no per-event
# id and neither does opencode's message, and inventing one for them would
# be a constraint over a value remem made up - which is how a legitimate
# repeat gets deleted.
#
# So for those, the answer is a report rather than a constraint. It is safe
# to key this on payload equality precisely because it only ever prints:
# every unkeyed payload's fields are stable (the volatile duration_ms lives
# on tool calls, which are keyed), and a false positive costs a line of
# output rather than an event.


def an_unkeyed_event(
    owner, *, harness="claude-code", session="s1", payload=None, at=NOW
):
    """A SessionEnd, the shape 011 deliberately cannot deduplicate."""
    return Event(
        id=new_id(),
        owner_id=owner.id,
        project="remem",
        harness=harness,
        session_id=session,
        kind=EventKind.SESSION_END,
        tool=None,
        payload=payload
        if payload is not None
        else {
            "hook_event_name": "SessionEnd",
            "reason": "clear",
            "session_id": session,
        },
        occurred_at=at,
    )


def test_status_reports_a_duplicated_unkeyed_event(store, owner):
    """The failure this exists for: a hook registered twice under two
    command names, recording every session close twice. That is exactly
    what happened to the claude-code adapter, and nothing anywhere
    noticed."""
    store.put_event(an_unkeyed_event(owner))
    store.put_event(an_unkeyed_event(owner, at=NOW + timedelta(seconds=1)))

    report = events.status(store, owner.id, idle_seconds=IDLE)

    assert len(report.suspected_duplicates) == 1
    dup = report.suspected_duplicates[0]
    assert dup.harness == "claude-code"
    assert dup.session_id == "s1"
    assert dup.count == 2


def test_a_single_unkeyed_event_is_not_reported(store, owner):
    """One SessionEnd per session is the normal shape and must stay quiet -
    a report that fires on healthy data is one nobody reads."""
    store.put_event(an_unkeyed_event(owner))

    report = events.status(store, owner.id, idle_seconds=IDLE)

    assert report.suspected_duplicates == []


def test_two_different_unkeyed_events_are_not_a_duplicate(store, owner):
    """A session can legitimately end more than once - `clear` then
    `prompt_input_exit`. Different payloads, not a duplicate."""
    store.put_event(an_unkeyed_event(owner, payload={"reason": "clear"}))
    store.put_event(
        an_unkeyed_event(
            owner,
            payload={"reason": "prompt_input_exit"},
            at=NOW + timedelta(seconds=1),
        )
    )

    report = events.status(store, owner.id, idle_seconds=IDLE)

    assert report.suspected_duplicates == []


def test_keyed_events_are_not_scanned_for_duplicates(store, owner):
    """011 already makes those impossible, so a second opinion here could
    only ever be wrong."""
    for i in range(2):
        e = an_event(owner)
        e.payload = {"command": "ls", "tool_use_id": f"tu_{i}"}
        store.put_event(e)

    report = events.status(store, owner.id, idle_seconds=IDLE)

    assert report.suspected_duplicates == []


def test_a_command_run_twice_is_never_reported_as_a_duplicate(store, owner):
    """The false positive that would make this report worthless.

    Running `ls` twice in a session is ordinary and the two events are
    byte-identical, so payload equality alone would flag them. Tool calls
    are excluded from the scan for exactly this reason - both harnesses
    that record them stamp a tool_use_id anyway.
    """
    for _ in range(2):
        store.put_event(an_event(owner))

    report = events.status(store, owner.id, idle_seconds=IDLE)

    assert report.suspected_duplicates == []


def test_the_duplicate_report_names_the_fix(store, owner):
    """The point is not to say a number - it is to tell the user that a
    hook is registered twice and which command re-registers it."""
    store.put_event(an_unkeyed_event(owner))
    store.put_event(an_unkeyed_event(owner, at=NOW + timedelta(seconds=1)))

    out = events.render(events.status(store, owner.id, idle_seconds=IDLE))

    assert "duplicate" in out.lower()
    assert "claude-code" in out
    assert "remem install claude-code" in out


def test_the_duplicate_report_reaches_json_too(store, owner):
    """--json and the human form read off one StatusReport; a detector
    visible in only one of them is how the two disagree."""
    store.put_event(an_unkeyed_event(owner))
    store.put_event(an_unkeyed_event(owner, at=NOW + timedelta(seconds=1)))

    payload = events.to_dict(events.status(store, owner.id, idle_seconds=IDLE))

    assert payload["suspected_duplicates"] == [
        {"project": "remem", "harness": "claude-code", "session_id": "s1", "count": 2}
    ]


def test_a_clean_status_says_nothing_about_duplicates(store, owner):
    store.put_event(an_event(owner))

    out = events.render(events.status(store, owner.id, idle_seconds=IDLE))

    assert "duplicate" not in out.lower()


def test_the_status_report_carries_hook_advisories(store, owner):
    """`record status` is what a user runs when a harness looks quiet, and
    is otherwise structurally incapable of answering - it reports what WAS
    recorded and cannot know what should have been. It is also the only
    place that can reach a harness which has recorded nothing ever and so
    appears nowhere in event_stats."""
    report = events.status(
        store,
        owner.id,
        idle_seconds=IDLE,
        hook_advisories=[
            "claude-code is installed but its hooks are "
            "incomplete: PostToolUse (missing) - run "
            "`remem doctor claude-code`"
        ],
    )
    text = events.render(report)
    assert "PostToolUse" in text
    assert "remem doctor claude-code" in text
    assert events.to_dict(report)["hook_advisories"] == report.hook_advisories


def test_a_healthy_install_adds_no_advisory_lines(store, owner):
    report = events.status(store, owner.id, idle_seconds=IDLE)
    assert report.hook_advisories == []
    assert "remem doctor" not in events.render(report)


def test_status_carries_an_ingest_advisory(store, owner):
    ingest.designate(store, owner.id, "remem", ["docs/specs"])
    run = store.start_ingest_run(owner.id, "remem", IngestTrigger.AUTO)
    store.finish_ingest_run(
        run.id,
        owner.id,
        created=0,
        changed=0,
        unchanged=0,
        swept=0,
        embedded=0,
        twins=[],
        embed_error=None,
        failures=[{"path": "docs/specs", "reason": "gone"}],
    )
    lines = ingest.advisories(store, owner.id, current_project=None, root=None)

    report = events.status(store, owner.id, idle_seconds=IDLE, ingest_advisories=lines)

    assert "! remem: last auto ingest" in events.render(report)
    assert events.to_dict(report)["ingest_advisories"] == lines
