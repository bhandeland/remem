"""The four spellings of a run history line. Pure - no database."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from remem.domain import MemoryRun, MemoryTrigger, new_id
from remem.services.memory import render_run

WHEN = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
SHOWN = WHEN.astimezone().strftime("%Y-%m-%d %H:%M")


def _run(**kw: Any) -> MemoryRun:
    # dict[str, Any] rather than an inferred type: a defaults table whose
    # values are deliberately heterogeneous infers as the union of them all,
    # and every field then fails to match its own parameter.
    base: dict[str, Any] = dict(
        id=new_id(),
        owner_id=new_id(),
        project="p",
        trigger=MemoryTrigger.MANUAL,
        started_at=WHEN,
        finished_at=WHEN,
    )
    return MemoryRun(**{**base, **kw})


def test_never_run():
    assert "never" in render_run(None).lower()


def test_finished_clean():
    line = render_run(_run(adopted=1, regenerated=2))
    assert "did not finish" not in line
    assert "conflict" not in line
    assert "1 adopted" in line and "2 regenerated" in line


def test_finished_with_conflicts():
    line = render_run(_run(conflicts=["a", "b"]))
    assert "2 conflict" in line


def test_finished_with_failures():
    line = render_run(_run(failures=[{"name": "x", "reason": "boom"}]))
    assert "1 failure" in line


def test_started_but_did_not_finish():
    line = render_run(_run(finished_at=None))
    assert "did not finish" in line


def test_the_four_spellings_are_distinct():
    lines = {
        render_run(None),
        render_run(_run()),
        render_run(_run(conflicts=["a"])),
        render_run(_run(finished_at=None)),
    }
    assert len(lines) == 4


def test_the_timestamp_is_shown_in_local_time():
    """The column is `timestamptz` and psycopg hands it back in UTC, so a
    bare strftime prints UTC's wall clock while `remem reingest status`,
    two screens away, prints local. Same fact, two different times."""
    assert SHOWN in render_run(_run())
    assert SHOWN in render_run(_run(finished_at=None))


def test_the_trigger_is_named():
    """Which half of the pipeline ran is not derivable from the counts, and
    an unattended `refresh` and a typed `sync` produce identical rows."""
    assert "auto" in render_run(_run(trigger=MemoryTrigger.AUTO))
    assert "manual" in render_run(_run(trigger=MemoryTrigger.MANUAL))


def test_the_trigger_is_named_on_a_run_that_did_not_finish():
    line = render_run(_run(trigger=MemoryTrigger.AUTO, finished_at=None))
    assert "auto" in line


def test_never_synced_names_no_trigger():
    """There is no run, so there is no trigger to name."""
    line = render_run(None)
    assert "auto" not in line and "manual" not in line
