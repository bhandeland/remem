"""`remem events prune` - the one command in this pipeline that deletes.

Deleting raw events is irreversible, so the command is built to refuse: no
default window, no unextracted events without `--force`, and it always
reports the provenance rows it leaves dangling.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from typer.testing import CliRunner

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.cli import app
from remem.domain import Event, EventKind, JobStatus, SessionRef, new_id
from remem.services import events, write

runner = CliRunner()

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


def _mark_done(store, owner, session_id, covers_through):
    """Give a session a done extract job with the given watermark, the same
    way `process` would after actually extracting it."""
    job = store.claim_extract_job(
        owner.id,
        SessionRef(
            project="remem", harness="claude-code", session_id=session_id,
            event_count=0, last_event_at=covers_through,
        ),
    )
    store.finish_extract_job(
        job.id, owner.id, JobStatus.DONE, None, 0, covers_through
    )


def test_prune_without_a_window_is_refused_at_the_cli(live_dsn, monkeypatch, tmp_path):
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))
    result = runner.invoke(app, ["events", "prune"])
    assert result.exit_code != 0
    assert "--before" in result.stdout + str(result.stderr)


def test_prune_refuses_unextracted_events(store, owner):
    old = an_event(owner, at=NOW - timedelta(days=40))
    store.put_event(old)

    with pytest.raises(events.PruneRefused) as exc:
        events.prune(store, owner.id, before=NOW)
    assert exc.value.unextracted == 1

    remaining = store.events_for_session(owner.id, "remem", "claude-code", "s1")
    assert [e.id for e in remaining] == [old.id]


def test_force_deletes_unextracted_events(store, owner):
    store.put_event(an_event(owner, at=NOW - timedelta(days=40)))

    report = events.prune(store, owner.id, before=NOW, force=True)

    assert report.deleted == 1
    assert store.events_for_session(owner.id, "remem", "claude-code", "s1") == []


def test_prune_leaves_entries_and_provenance_intact(store, owner):
    entry = write.remember(store, owner.id, title="t", body="b")
    event = store.put_event(an_event(owner, at=NOW - timedelta(days=40)))
    store.link_entry_events(entry.id, [event], owner.id)
    _mark_done(store, owner, "s1", covers_through=event.occurred_at)

    report = events.prune(store, owner.id, before=NOW)

    assert report.deleted == 1
    assert store.get_entry(entry.id, owner.id) is not None
    assert store.provenance(entry.id, owner.id) == [
        (event.id, "s1", "claude-code", False)
    ]


def test_prune_reports_what_it_left_dangling(store, owner):
    entry = write.remember(store, owner.id, title="t", body="b")
    old_events = [
        store.put_event(an_event(owner, at=NOW - timedelta(days=40, minutes=i)))
        for i in range(3)
    ]
    store.link_entry_events(entry.id, old_events, owner.id)
    _mark_done(store, owner, "s1", covers_through=NOW - timedelta(days=39))

    report = events.prune(store, owner.id, before=NOW)

    assert report.deleted == 3
    assert report.dangling == 3


def test_events_inside_the_window_are_kept(store, owner):
    old = store.put_event(an_event(owner, at=NOW - timedelta(days=40)))
    recent = store.put_event(
        an_event(owner, at=NOW - timedelta(days=1), session="s1")
    )
    _mark_done(store, owner, "s1", covers_through=NOW)

    report = events.prune(store, owner.id, before=NOW - timedelta(days=30))

    assert report.deleted == 1
    remaining = store.events_for_session(owner.id, "remem", "claude-code", "s1")
    assert [e.id for e in remaining] == [recent.id]


def test_a_mixed_window_prunes_what_it_can_without_refusing(store, owner):
    """One session already extracted, one still not: the extracted session's
    events go, the other is silently skipped, and the run succeeds - a
    refusal is reserved for a run that would otherwise delete nothing at
    all, not for one that partially can't."""
    extracted = store.put_event(
        an_event(owner, at=NOW - timedelta(days=40), session="done")
    )
    unextracted = store.put_event(
        an_event(owner, at=NOW - timedelta(days=40), session="stuck")
    )
    _mark_done(store, owner, "done", covers_through=extracted.occurred_at)

    report = events.prune(store, owner.id, before=NOW)

    assert report.deleted == 1
    assert report.kept_unextracted == 1
    assert store.events_for_session(
        owner.id, "remem", "claude-code", "done"
    ) == []
    assert [
        e.id for e in store.events_for_session(
            owner.id, "remem", "claude-code", "stuck"
        )
    ] == [unextracted.id]


def test_prune_never_reaches_across_owners(store, owner):
    """`prune_events` is the only owner-wide DELETE in the codebase, and its
    window is a timestamp - the owner predicate in the `scoped` CTE is the
    entire thing standing between one principal's retention run and every
    other principal's raw. Nothing else pins it."""
    other = store.ensure_principal("someone-else")
    mine = store.put_event(an_event(owner, at=NOW - timedelta(days=40)))
    theirs = store.put_event(an_event(other, at=NOW - timedelta(days=40)))
    _mark_done(store, owner, "s1", covers_through=mine.occurred_at)
    _mark_done(store, other, "s1", covers_through=theirs.occurred_at)

    report = events.prune(store, owner.id, before=NOW)

    assert report.deleted == 1
    assert store.events_for_session(owner.id, "remem", "claude-code", "s1") == []
    survived = store.events_for_session(other.id, "remem", "claude-code", "s1")
    assert [e.id for e in survived] == [theirs.id]


def _seed_prunable(dsn):
    """One old, already-extracted event, committed - ready to be deleted."""
    with psycopg.connect(dsn) as c:
        store = PostgresStore(c)
        owner = store.ensure_principal("brandon")
        event = store.put_event(an_event(owner, at=NOW - timedelta(days=40)))
        _mark_done(store, owner, "s1", covers_through=event.occurred_at)
        c.commit()
        return owner.id


@pytest.fixture
def cli_env(live_dsn, monkeypatch, tmp_path):
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))
    return live_dsn


def test_prune_deletes_through_the_cli(cli_env):
    """The refusal paths were the only ones the CLI covered. This is the run
    that actually commits a delete - the whole reason the command exists, and
    the one whose rows do not come back."""
    owner_id = _seed_prunable(cli_env)

    result = runner.invoke(app, ["events", "prune", "--before", "30d"])

    assert result.exit_code == 0, result.stdout + str(result.stderr)
    assert "deleted 1 events" in result.stdout
    with psycopg.connect(cli_env) as c:
        store = PostgresStore(c)
        assert store.events_for_session(
            owner_id, "remem", "claude-code", "s1"
        ) == []


def test_prune_json_reports_all_three_counts(cli_env):
    """--json is what a cron wrapper reads, so every count the human line
    prints has to be in it - a silently absent `dangling` reads as zero."""
    _seed_prunable(cli_env)

    result = runner.invoke(app, ["events", "prune", "--before", "30d", "--json"])

    assert result.exit_code == 0, result.stdout + str(result.stderr)
    assert json.loads(result.stdout) == {
        "deleted": 1, "kept_unextracted": 0, "dangling": 0
    }


@pytest.mark.parametrize(
    "text,expected",
    [
        ("30d", timedelta(days=30)),
        ("12h", timedelta(hours=12)),
        ("90m", timedelta(minutes=90)),
    ],
)
def test_window_parsing(text, expected):
    assert events.parse_window(text) == expected


@pytest.mark.parametrize("text", ["", "30", "d30", "-5d", "30 days", "0d"])
def test_a_window_that_does_not_parse_is_refused(text):
    """Including "30" with no unit. Guessing a unit for a bare number is how
    a user who meant 30 days deletes 30 minutes' worth - or everything."""
    with pytest.raises(events.BadWindow):
        events.parse_window(text)
