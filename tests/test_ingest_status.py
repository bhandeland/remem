"""`remem reingest status` and the advisory line in `remem record status`.

The refresh is fail-soft and detached; these are the on-demand answer to
"did it run, and did it work". The render is pure so its four last-run
states are tested without a database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from remem.domain import IngestDesignation, IngestRun, IngestTrigger
from remem.services import ingest

AT = datetime(2026, 9, 4, 14, 2, tzinfo=timezone.utc)
SHOWN = AT.astimezone().strftime("%Y-%m-%d %H:%M")


def _run(**kw) -> IngestRun:
    base = dict(
        id=uuid4(),
        owner_id=uuid4(),
        project="remem",
        trigger=IngestTrigger.AUTO,
        started_at=AT,
        finished_at=AT,
    )
    base.update(kw)
    return IngestRun(**base)


def _status(**kw) -> ingest.ProjectIngestStatus:
    base = dict(
        project="remem",
        designations=[IngestDesignation("remem", ("docs/specs", "docs/notes"))],
        last_run=None,
        checked_against=None,
        missing=[],
    )
    base.update(kw)
    return ingest.ProjectIngestStatus(**base)


# ---- pure ----


def test_render_lists_designations_and_never_run():
    out = ingest.render_status([_status()], "remem")
    assert "remem (default): docs/specs, docs/notes" in out
    assert "last run: never. A session start inside this repository spawns one." in out


def test_render_a_clean_finished_run():
    out = ingest.render_status(
        [
            _status(
                last_run=_run(created=3, changed=1, unchanged=40, swept=0, embedded=4)
            )
        ],
        "remem",
    )
    assert (
        f"last run: auto, {SHOWN}, 3 new, 1 changed, 40 unchanged, 0 swept, 4 embedded"
        in out
    )
    assert "failed:" not in out


def test_render_a_run_with_failures_twins_and_an_embed_error():
    run = _run(
        failures=[{"path": "docs/notes", "reason": "No such file"}],
        twins=[{"path": "docs/a.md", "existing": "notes/docs/a.md", "live": 12}],
        embed_error="fastembed is not installed",
    )
    out = ingest.render_status([_status(last_run=run)], "remem")
    assert "  failed: docs/notes: No such file" in out
    assert "  twin: docs/a.md is new, but src:notes/docs/a.md has 12 live chunks" in out
    assert "  embed skipped: fastembed is not installed" in out


def test_render_an_unfinished_run():
    out = ingest.render_status([_status(last_run=_run(finished_at=None))], "remem")
    assert f"last run: auto, started {SHOWN}, did not finish" in out


def test_render_names_where_the_disk_check_looked():
    out = ingest.render_status(
        [_status(checked_against=Path("/repo"), missing=["docs/notes"])],
        "remem",
    )
    assert "missing on disk: docs/notes  (checked against /repo)" in out


def test_render_says_when_paths_were_not_checked():
    out = ingest.render_status([_status(checked_against=None)], "remem")
    assert "paths not checked: run from inside remem's repository" in out


def test_render_a_clean_disk_check_says_so():
    out = ingest.render_status([_status(checked_against=Path("/repo"))], "remem")
    assert "all designated paths present  (checked against /repo)" in out


def test_render_nothing_designated():
    assert "not designated" in ingest.render_status([], "remem")
    assert "This project" in ingest.render_status([], None)


def test_render_an_undesignated_projects_manual_run_has_no_check_line():
    """The `status()` fallback for a project that was manually ingested but
    never designated: `designations=[]`, `checked_against=None`. Nothing
    was designated, so neither "all present" nor "not checked" may appear -
    both describe a check that never ran, and would read as a clean pass
    over an empty one."""
    out = ingest.render_status(
        [
            _status(
                designations=[],
                last_run=_run(trigger=IngestTrigger.MANUAL),
                checked_against=None,
            )
        ],
        "remem",
    )
    assert (
        "remem is not designated for automatic re-ingest. Designate it "
        "with `remem reingest designate <paths>`."
    ) in out
    assert "last run: manual" in out
    assert "all designated paths present" not in out
    assert "paths not checked" not in out


def test_status_to_dict_carries_the_run_and_the_check():
    [d] = ingest.status_to_dict(
        [
            _status(
                last_run=_run(created=1), checked_against=Path("/repo"), missing=["x"]
            )
        ]
    )
    assert d["project"] == "remem"
    assert d["designations"] == [
        {"archive": False, "paths": ["docs/specs", "docs/notes"]}
    ]
    assert d["last_run"]["trigger"] == "auto"
    assert d["last_run"]["created"] == 1
    assert d["last_run"]["started_at"] == AT.isoformat()
    assert d["check"] == {"checked_against": "/repo", "missing": ["x"]}


# ---- db ----


@pytest.fixture
def store(conn):
    from remem.backends.postgres.migrate import migrate
    from remem.backends.postgres.store import PostgresStore

    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


@pytest.fixture
def root(tmp_path):
    (tmp_path / "docs" / "specs").mkdir(parents=True)
    (tmp_path / "docs" / "specs" / "one.md").write_text("# One\n\nbody\n")
    return tmp_path


@pytest.mark.db
def test_status_checks_disk_only_for_the_current_project(store, owner, root):
    ingest.designate(store, owner.id, "here", ["docs/specs", "docs/gone"])
    ingest.designate(store, owner.id, "there", ["docs/gone"])

    found = {
        s.project: s
        for s in ingest.status(store, owner.id, None, current_project="here", root=root)
    }

    assert found["here"].checked_against == root
    assert found["here"].missing == ["docs/gone"]
    assert found["there"].checked_against is None
    assert found["there"].missing == []


@pytest.mark.db
def test_status_for_one_project_carries_its_latest_run(store, owner, root):
    ingest.designate(store, owner.id, "here", ["docs/specs"])
    run = store.start_ingest_run(owner.id, "here", IngestTrigger.AUTO)

    [found] = ingest.status(store, owner.id, "here", current_project=None, root=None)

    assert found.last_run.id == run.id
    assert found.checked_against is None


@pytest.mark.db
def test_advisories_name_only_the_unhealthy_projects(store, owner, root):
    ingest.designate(store, owner.id, "clean", ["docs/specs"])
    ingest.designate(store, owner.id, "failed", ["docs/specs"])
    ingest.designate(store, owner.id, "stuck", ["docs/specs"])
    ingest.designate(store, owner.id, "renamed", ["docs/gone"])
    for project in ("clean", "failed"):
        run = store.start_ingest_run(owner.id, project, IngestTrigger.AUTO)
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
            failures=[] if project == "clean" else [{"path": "x", "reason": "boom"}],
        )
    store.start_ingest_run(owner.id, "stuck", IngestTrigger.AUTO)

    lines = ingest.advisories(store, owner.id, current_project="renamed", root=root)

    assert len(lines) == 3
    assert any(
        ln.startswith("failed:")
        and "1 failure(s)" in ln
        and ln.endswith("see: remem reingest status --project failed")
        for ln in lines
    )
    assert any(ln.startswith("stuck:") and "did not finish" in ln for ln in lines)
    assert any(ln.startswith("renamed:") and "docs/gone" in ln for ln in lines)
    assert not any(ln.startswith("clean:") for ln in lines)


@pytest.mark.db
def test_advisories_are_empty_with_nothing_designated(store, owner, root):
    assert ingest.advisories(store, owner.id, current_project=None, root=None) == []


@pytest.mark.db
def test_advisories_ignore_an_undesignated_projects_failed_manual_run(
    store, owner, root
):
    """status()'s fallback surfaces an undesignated current project's run
    for `reingest status` - right there, since that screen is answering
    "what happened here". But the spec scopes this advisory to designated
    projects: `remem ingest` is already fail-loud about its own failures,
    and an undesignated project's run row never repairs itself, so
    repeating it here would be a permanently stuck line whose pointer
    (remem reingest status --project solo) contradicts itself from outside
    this directory, where the fallback's `project == current_project`
    guard fails and the screen reads "not designated" with no run shown.
    """
    run = store.start_ingest_run(owner.id, "solo", IngestTrigger.MANUAL)
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
        failures=[{"path": "x", "reason": "boom"}],
    )

    lines = ingest.advisories(store, owner.id, current_project="solo", root=root)

    assert lines == []
