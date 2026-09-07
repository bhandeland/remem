"""The four spellings of a run history line. Pure - no database."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from remem.domain import MemoryRun, MemoryTrigger, new_id
from remem.services.memory import Status, render_run, status_to_dict

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


def _status(**kw: Any) -> Status:
    base: dict[str, Any] = dict(
        project="p",
        collection="p-memory",
        directory=Path("/tmp/mem"),
    )
    return Status(**{**base, **kw})


def test_status_to_dict_carries_every_field():
    d = status_to_dict(
        _status(entries=3, files=4, stale=1, overlap=2, overlap_bytes=99, conflicts=5)
    )
    assert d["project"] == "p"
    assert d["collection"] == "p-memory"
    assert d["directory"] == "/tmp/mem"
    assert d["entries"] == 3
    assert d["files"] == 4
    assert d["stale"] == 1
    assert d["conflicts"] == 5
    assert d["overlap"] == {"entries": 2, "bytes": 99}


def test_status_to_dict_carries_the_run():
    run = _run(trigger=MemoryTrigger.AUTO, adopted=1, renamed=[["a", "b"]])
    d = status_to_dict(_status(run=run))
    assert d["run"] is not None
    assert d["run"]["trigger"] == "auto"
    assert d["run"]["adopted"] == 1
    assert d["run"]["renamed"] == [["a", "b"]]
    assert d["run"]["started_at"] == WHEN.isoformat()
    assert d["run"]["id"] == str(run.id)


def test_an_undesignated_project_has_the_same_keys():
    """A consumer must not have to branch on which keys exist. The text
    output early-returns one line here; --json keeps one shape and says
    `null`, which is the one place the two deliberately differ."""
    designated = status_to_dict(_status(run=_run()))
    undesignated = status_to_dict(_status(collection=None, directory=None))
    assert undesignated.keys() == designated.keys()
    assert undesignated["collection"] is None
    assert undesignated["directory"] is None
    assert undesignated["run"] is None


def test_status_to_dict_is_json_serialisable():
    """Every value has to survive json.dumps - a UUID or a datetime left
    unconverted raises only at the moment the command is run."""
    json.dumps(status_to_dict(_status(run=_run(finished_at=None))))
