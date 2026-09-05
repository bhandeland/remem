"""The four spellings of a run history line. Pure - no database."""

from __future__ import annotations

from datetime import datetime, timezone

from remem.domain import MemoryRun, MemoryTrigger, new_id
from remem.services.memory import render_run

WHEN = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def _run(**kw):
    base = dict(id=new_id(), owner_id=new_id(), project="p",
                trigger=MemoryTrigger.MANUAL, started_at=WHEN,
                finished_at=WHEN)
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
