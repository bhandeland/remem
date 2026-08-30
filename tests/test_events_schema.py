"""What 008 creates, and the constraint it deliberately omits.

The missing foreign key from entry_events.event_id to events.id is the
load-bearing omission in this schema. A test asserts its absence, because a
later reader "fixing" it would make prune choose between blocking and
erasing provenance - and both are worse than a pointer to something we
deleted on purpose.
"""

from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate

pytestmark = pytest.mark.db


@pytest.fixture
def migrated(conn):
    migrate(conn)
    return conn


def test_events_and_entry_events_exist(migrated):
    for table in ("events", "entry_events"):
        assert migrated.execute(
            "select to_regclass(%s)", (f"public.{table}",)
        ).fetchone()[0] is not None


def test_event_kind_is_small_and_closed(migrated):
    labels = migrated.execute(
        "select enumlabel from pg_enum e join pg_type t on t.oid = e.enumtypid"
        " where t.typname = 'event_kind' order by enumsortorder"
    ).fetchall()
    assert [r[0] for r in labels] == ["tool_call", "message", "session_end"]


def test_entry_events_has_no_foreign_key_to_events(migrated):
    """Deliberate. See 008_events.sql for why, before removing this test."""
    fks = migrated.execute(
        "select conname from pg_constraint"
        " where conrelid = 'entry_events'::regclass and contype = 'f'"
    ).fetchall()
    referenced = [r[0] for r in fks]
    assert not any("event" in name and "entry_id" not in name
                   for name in referenced)


def test_deleting_an_entry_deletes_its_provenance(migrated):
    """entry_id DOES cascade - the entry is the thing the row is about."""
    fks = migrated.execute(
        "select confdeltype from pg_constraint"
        " where conrelid = 'entry_events'::regclass and contype = 'f'"
    ).fetchall()
    assert [r[0] for r in fks] == ["c"]


def test_event_id_is_indexed(migrated):
    """No foreign key means no index for free, and 'what came out of this
    event' would be a sequential scan without one."""
    indexes = migrated.execute(
        "select indexdef from pg_indexes where tablename = 'entry_events'"
    ).fetchall()
    assert any("event_id" in r[0] and "entry_events_event_idx" in r[0]
               for r in indexes)


def test_event_key_is_null_when_the_harness_supplies_no_id(migrated):
    """The partial index has to stay partial.

    claude-code's SessionEnd payload carries no per-event id and neither
    does opencode's message, so most of what remem records cannot be
    deduplicated at all. A key that invented something for those rows
    would be a constraint over a value remem made up - which is how a
    legitimate repeat gets dropped.
    """
    row = migrated.execute(
        "select event_key from (select %s::jsonb as payload) p"
        " cross join lateral (select case"
        "   when p.payload->>'tool_use_id' is not null"
        "     then 'tool:' || (p.payload->>'tool_use_id')"
        "   when p.payload->>'generation_id' is not null"
        "     then 'gen:' || (p.payload->>'generation_id')"
        "          || ':' || coalesce(p.payload->>'hook_event_name', '')"
        " end as event_key) k",
        ('{"hook_event_name": "SessionEnd", "reason": "clear"}',),
    ).fetchone()
    assert row[0] is None


def test_the_uniqueness_index_on_events_is_partial(migrated):
    """A total index would make every id-less event collide with the next.

    Every claude-code SessionEnd has a null key; under a total unique index
    the second one in a session would be silently discarded.
    """
    predicate = migrated.execute(
        "select pg_get_expr(indpred, indrelid) from pg_index i"
        " join pg_class c on c.oid = i.indexrelid"
        " where c.relname = 'events_harness_key_uniq'"
    ).fetchone()
    assert predicate is not None, "events_harness_key_uniq is missing"
    assert predicate[0] is not None, "the index must be partial, not total"
