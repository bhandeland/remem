# Ingest Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make automatic re-ingest leave a record, make its failures visible on demand, and stop `remem ingest` from duplicating a document when run from anywhere but the repository root.

**Architecture:** A new `ingest_runs` table holds one row per ingest invocation, written by `services/ingest.py` for both the spawned refresh and the manual command. `remem reingest status` renders the latest row plus an on-disk check of designated paths; `remem record status` carries one advisory line per project whose latest run is unhealthy. Manual `remem ingest` computes chunk identity relative to the working tree's top level, and a per-file twin warning flags a new file whose filename already has live chunks under another `src:` path.

**Tech Stack:** Python 3.14, psycopg 3 with hand-written SQL, Typer, pytest with the `db` marker for anything that touches Postgres.

**Spec:** `docs/superpowers/specs/2026-09-04-ingest-observability-design.md`

## Global Constraints

- Strict layering: `cli.py` parses and formats, never decides. Every policy branch goes in `services/ingest.py`.
- Nothing outside `session.py` and `backends/` imports psycopg.
- Migrations: add `017_ingest_runs.sql`, never edit an applied file. Timestamps use `clock_timestamp()`.
- Ownership is enforced inside the store (`NotOwner`), not by callers.
- `reingest run` keeps its contract: exit 0 on every path, nothing on stdout, stderr only behind `REMEM_HOOK_DEBUG`.
- `remem ingest` stays fail-loud: failures exit 1. A twin warning does not change the exit code.
- Comments explain *why*, at length. Prose uses spaced hyphens ` - `, never em dashes.
- Tests that need Postgres carry `pytestmark = pytest.mark.db`. Pure tests carry no marker. Every `db` test that asserts a delete or an owner filter seeds a second principal's rows and asserts they survive by id.
- After writing each guard, revert the defect it targets on a scratch copy and watch it go red before trusting it.
- A green run counts only with a zero skip count. Check `docker compose ps` and the skip count.
- Commit after every task with the message given.

---

## File map

| file | responsibility after this plan |
|---|---|
| `src/remem/backends/postgres/migrations/017_ingest_runs.sql` | the `ingest_runs` table and its one index |
| `src/remem/domain.py` | `IngestTrigger` enum and `IngestRun` dataclass |
| `src/remem/store.py` | Protocol: `start_ingest_run`, `finish_ingest_run`, `latest_ingest_run`, `anchors` |
| `src/remem/backends/postgres/store.py` | the four implementations, `INGEST_RUN_FIELDS`, `_row_to_ingest_run` |
| `src/remem/project.py` | `toplevel(start)` |
| `src/remem/services/ingest.py` | `Report.twins`, `find_twin`, twin check inside `ingest_file`, `relative_to_root`, `ingest_manual`, run rows in `refresh`, `ProjectIngestStatus`, `status`, `render_status`, `status_to_dict`, `advisories` |
| `src/remem/services/events.py` | `StatusReport.ingest_advisories`, rendered and in `to_dict` |
| `src/remem/cli.py` | `ingest` translates arguments, prints twins; `reingest status` renders the status, `--json`; `reingest run` uses autocommit; `record status` passes ingest advisories |
| `CLAUDE.md` | a paragraph under "Ingested documents" |
| `tests/test_store_ingest_runs.py` | store round trip for run rows and `anchors` (db) |
| `tests/test_ingest_twins.py` | `find_twin` (pure) and the twin check (db) |
| `tests/test_ingest_identity.py` | `toplevel` and `relative_to_root` (pure, uses a scratch git repo) |
| `tests/test_ingest_status.py` | `render_status` (pure) and `status`/`advisories` (db) |
| `tests/test_ingest_refresh.py` | run rows from `refresh` (db, existing file) |
| `tests/test_ingest_cli.py` | manual run row, identity from a subdirectory, twin line (db, existing file) |
| `tests/test_reingest_cli.py` | `reingest status` rendering and `--json` (db, existing file) |
| `tests/test_record_status.py` | the ingest advisory line (db, existing file) |

---

### Task 1: The `ingest_runs` table, domain types, and store round trip

**Files:**
- Create: `src/remem/backends/postgres/migrations/017_ingest_runs.sql`
- Modify: `src/remem/domain.py` (after `IngestDesignation`, line ~190)
- Modify: `src/remem/store.py` (after `ingest_designations`, line ~115)
- Modify: `src/remem/backends/postgres/store.py` (imports at top; new methods after `ingest_designations`, line ~770)
- Test: `tests/test_store_ingest_runs.py`

**Interfaces:**
- Produces:
  - `domain.IngestTrigger(StrEnum)`: `AUTO = "auto"`, `MANUAL = "manual"`
  - `domain.IngestRun` dataclass (fields below)
  - `Store.start_ingest_run(owner_id: UUID, project: str, trigger: IngestTrigger, archive: bool = False) -> IngestRun`
  - `Store.finish_ingest_run(run_id: UUID, owner_id: UUID, *, created: int, changed: int, unchanged: int, swept: int, embedded: int, failures: list[dict], twins: list[dict], embed_error: str | None) -> None` - raises `NotOwner` when the row is not the owner's
  - `Store.latest_ingest_run(owner_id: UUID, project: str) -> IngestRun | None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_store_ingest_runs.py
"""Run rows: the after-the-fact record of an ingest.

The refresh is fail-soft and detached, so this table is the only thing on
the machine that can say whether it ran. A started row with no finish is a
statement - the process died - and must read back that way.
"""

from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import IngestTrigger
from remem.store import NotOwner

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


@pytest.fixture
def other(store):
    return store.ensure_principal("someone-else")


def test_a_started_row_reads_back_unfinished(store, owner):
    run = store.start_ingest_run(owner.id, "proj", IngestTrigger.AUTO)

    latest = store.latest_ingest_run(owner.id, "proj")
    assert latest is not None
    assert latest.id == run.id
    assert latest.trigger is IngestTrigger.AUTO
    assert latest.started_at is not None
    assert latest.finished_at is None
    assert latest.failures == []


def test_finish_records_counts_failures_twins_and_the_embed_error(store, owner):
    run = store.start_ingest_run(owner.id, "proj", IngestTrigger.MANUAL, archive=True)

    store.finish_ingest_run(
        run.id, owner.id,
        created=3, changed=1, unchanged=40, swept=2, embedded=4,
        failures=[{"path": "docs/gone", "reason": "No such file"}],
        twins=[{"path": "docs/a.md", "existing": "notes/docs/a.md", "live": 12}],
        embed_error="fastembed is not installed",
    )

    latest = store.latest_ingest_run(owner.id, "proj")
    assert latest.finished_at is not None
    assert latest.archive is True
    assert (latest.created, latest.changed, latest.unchanged, latest.swept,
            latest.embedded) == (3, 1, 40, 2, 4)
    assert latest.failures == [{"path": "docs/gone", "reason": "No such file"}]
    assert latest.twins == [
        {"path": "docs/a.md", "existing": "notes/docs/a.md", "live": 12}
    ]
    assert latest.embed_error == "fastembed is not installed"


def test_latest_is_the_newest_started(store, owner):
    first = store.start_ingest_run(owner.id, "proj", IngestTrigger.AUTO)
    second = store.start_ingest_run(owner.id, "proj", IngestTrigger.AUTO)

    assert store.latest_ingest_run(owner.id, "proj").id == second.id
    assert first.id != second.id


def test_latest_is_per_project_and_per_owner(store, owner, other):
    store.start_ingest_run(owner.id, "proj", IngestTrigger.AUTO)
    theirs = store.start_ingest_run(other.id, "proj", IngestTrigger.AUTO)

    assert store.latest_ingest_run(owner.id, "other-proj") is None
    assert store.latest_ingest_run(other.id, "proj").id == theirs.id
    assert store.latest_ingest_run(owner.id, "proj").id != theirs.id


def test_finishing_someone_elses_row_raises_not_owner(store, owner, other):
    theirs = store.start_ingest_run(other.id, "proj", IngestTrigger.AUTO)

    with pytest.raises(NotOwner):
        store.finish_ingest_run(
            theirs.id, owner.id,
            created=0, changed=0, unchanged=0, swept=0, embedded=0,
            failures=[], twins=[], embed_error=None,
        )
    assert store.latest_ingest_run(other.id, "proj").finished_at is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_store_ingest_runs.py -v`
Expected: FAIL with `ImportError: cannot import name 'IngestTrigger'`. If the output says SKIPPED, Postgres is not up: `docker compose up -d` and run again.

- [ ] **Step 3: Write the migration**

```sql
-- src/remem/backends/postgres/migrations/017_ingest_runs.sql
--
-- One row per ingest invocation - the spawned `remem reingest run` and the
-- typed `remem ingest` alike. This is the after-the-fact record a fail-soft,
-- detached refresh otherwise never leaves: before this table, `refresh`
-- computed counts, per-path failures and the embed error and handed them to
-- a debug channel whose stderr is /dev/null.
--
-- A started row with `finished_at` null is a statement, not a gap: the
-- process died between starting and finishing. That is what makes "crashed"
-- distinguishable from "never ran". It only works because the spawned run
-- commits the started row before doing any work (autocommit, like
-- `remem events process`).
--
-- Rows are kept indefinitely, as events are. Nothing prunes them.
create table ingest_runs (
  id uuid primary key,
  owner_id uuid not null references principals(id),
  project text not null,
  -- 'auto' for the spawned refresh, 'manual' for `remem ingest`. Text with
  -- a check rather than an enum: two values, and the check reads the same.
  trigger text not null check (trigger in ('auto', 'manual')),
  -- Which origin a manual run wrote. The refresh covers both halves in one
  -- row and records false.
  archive boolean not null default false,
  started_at timestamptz not null default clock_timestamp(),
  finished_at timestamptz,
  created int not null default 0,
  changed int not null default 0,
  unchanged int not null default 0,
  swept int not null default 0,
  embedded int not null default 0,
  -- [{"path": ..., "reason": ...}]. A designated path that no longer exists
  -- lands here on every run, which is how a renamed directory stops being
  -- a silent per-run failure.
  failures jsonb not null default '[]'::jsonb,
  -- [{"path": ..., "existing": ..., "live": n}] - files that came in
  -- entirely new while an anchor with the same filename already had live
  -- chunks under another src: path. See services/ingest.find_twin.
  twins jsonb not null default '[]'::jsonb,
  embed_error text
);

-- Every read is "the latest row for this project".
create index ingest_runs_latest_idx
  on ingest_runs (owner_id, project, started_at desc);
```

- [ ] **Step 4: Add the domain types**

In `src/remem/domain.py`, immediately after the `IngestDesignation` dataclass (it ends with `archive: bool = False`), add:

```python
class IngestTrigger(StrEnum):
    """Who started an ingest run: the spawned refresh, or a person."""

    AUTO = "auto"
    MANUAL = "manual"


@dataclass(slots=True)
class IngestRun:
    """One ingest invocation's record - see 017_ingest_runs.sql.

    `finished_at` is None for a row whose process died before finishing.
    `failures` and `twins` are lists of plain dicts, the shape they are
    stored in, because the only readers are a status renderer and `--json`.
    """

    id: UUID
    owner_id: UUID
    project: str
    trigger: IngestTrigger
    archive: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created: int = 0
    changed: int = 0
    unchanged: int = 0
    swept: int = 0
    embedded: int = 0
    failures: list[dict] = field(default_factory=list)
    twins: list[dict] = field(default_factory=list)
    embed_error: str | None = None
```

Check that `field` is already imported from `dataclasses` at the top of `domain.py` (`CollectionQuery` uses it). If not, add it.

- [ ] **Step 5: Add the Protocol methods**

In `src/remem/store.py`, after `ingest_designations` in the `# ingest designations` block, add:

```python
    # ingest runs
    #: Opens a row and returns it. Called before any file is read, so that a
    #: process which dies mid-run leaves a started, unfinished row behind.
    def start_ingest_run(
        self, owner_id: UUID, project: str, trigger: IngestTrigger,
        archive: bool = False,
    ) -> IngestRun: ...
    #: Records the outcome. Raises NotOwner for a row that is not the
    #: caller's - ownership is enforced here, not by callers.
    def finish_ingest_run(
        self, run_id: UUID, owner_id: UUID, *,
        created: int, changed: int, unchanged: int, swept: int, embedded: int,
        failures: list[dict], twins: list[dict], embed_error: str | None,
    ) -> None: ...
    #: The newest-started row for one project, finished or not.
    def latest_ingest_run(
        self, owner_id: UUID, project: str
    ) -> IngestRun | None: ...
```

Add `IngestRun` and `IngestTrigger` to the `from remem.domain import (...)` list at the top of `store.py`.

- [ ] **Step 6: Implement in the Postgres store**

In `src/remem/backends/postgres/store.py`, add `IngestRun` and `IngestTrigger` to the `from remem.domain import (...)` block. After `_row_to_extract_job` (line ~162), add:

```python
INGEST_RUN_FIELDS = [
    "id", "owner_id", "project", "trigger", "archive", "started_at",
    "finished_at", "created", "changed", "unchanged", "swept", "embedded",
    "failures", "twins", "embed_error",
]


def ingest_run_columns(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{f}" for f in INGEST_RUN_FIELDS)


def _row_to_ingest_run(row: dict) -> IngestRun:
    return IngestRun(
        id=row["id"],
        owner_id=row["owner_id"],
        project=row["project"],
        trigger=IngestTrigger(row["trigger"]),
        archive=row["archive"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        created=row["created"],
        changed=row["changed"],
        unchanged=row["unchanged"],
        swept=row["swept"],
        embedded=row["embedded"],
        failures=list(row["failures"]),
        twins=list(row["twins"]),
        embed_error=row["embed_error"],
    )
```

After the `ingest_designations` method (line ~770), add:

```python
    # ---------------- ingest runs ----------------

    def start_ingest_run(
        self, owner_id: UUID, project: str, trigger: IngestTrigger,
        archive: bool = False,
    ) -> IngestRun:
        run_id = new_id()
        with self._cur() as cur:
            cur.execute(
                f"""
                insert into ingest_runs (id, owner_id, project, trigger, archive)
                values (%s, %s, %s, %s, %s)
                returning {ingest_run_columns()}
                """,
                (run_id, owner_id, project, str(trigger), archive),
            )
            return _row_to_ingest_run(cur.fetchone())

    def finish_ingest_run(
        self, run_id: UUID, owner_id: UUID, *,
        created: int, changed: int, unchanged: int, swept: int, embedded: int,
        failures: list[dict], twins: list[dict], embed_error: str | None,
    ) -> None:
        with self._cur() as cur:
            cur.execute(
                """
                update ingest_runs
                   set finished_at = clock_timestamp(),
                       created = %s, changed = %s, unchanged = %s,
                       swept = %s, embedded = %s,
                       failures = %s::jsonb, twins = %s::jsonb,
                       embed_error = %s
                 where id = %s and owner_id = %s
                """,
                (created, changed, unchanged, swept, embedded,
                 json.dumps(failures), json.dumps(twins), embed_error,
                 run_id, owner_id),
            )
            if cur.rowcount == 0:
                raise NotOwner(f"ingest run {run_id} is not owned by {owner_id}")

    def latest_ingest_run(
        self, owner_id: UUID, project: str
    ) -> IngestRun | None:
        with self._cur() as cur:
            cur.execute(
                f"""
                select {ingest_run_columns()} from ingest_runs
                 where owner_id = %s and project = %s
                 order by started_at desc
                 limit 1
                """,
                (owner_id, project),
            )
            row = cur.fetchone()
        return _row_to_ingest_run(row) if row else None
```

Check how `NotOwner` is raised elsewhere in this file (`grep -n "raise NotOwner" src/remem/backends/postgres/store.py`) and match its message style.

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_store_ingest_runs.py tests/test_migrate.py -v`
Expected: all PASS, 0 skipped.

- [ ] **Step 8: Watch the owner guard fail**

Temporarily delete the `if cur.rowcount == 0: raise NotOwner(...)` lines, run `uv run pytest tests/test_store_ingest_runs.py::test_finishing_someone_elses_row_raises_not_owner`, confirm it FAILS, restore the lines, confirm it passes.

- [ ] **Step 9: Commit**

```bash
git add src/remem/backends/postgres/migrations/017_ingest_runs.sql src/remem/domain.py src/remem/store.py src/remem/backends/postgres/store.py tests/test_store_ingest_runs.py
git commit -m "Add ingest_runs: one row per ingest invocation"
```

---

### Task 2: `Store.anchors`

**Files:**
- Modify: `src/remem/store.py` (after `latest_ingest_run`)
- Modify: `src/remem/backends/postgres/store.py` (after `latest_ingest_run`)
- Test: `tests/test_store_ingest_runs.py` (append)

**Interfaces:**
- Produces: `Store.anchors(owner_id: UUID, project: str) -> list[Entry]` - live `INGESTED`/`ARCHIVED` entries in the project with a `src:` tag and no `sec:` tag, newest first.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_store_ingest_runs.py`:

```python
from pathlib import Path

from remem.domain import Kind, Origin
from remem.services import ingest, write


def _ingest(store, owner, tmp_path, rel, project="proj", archive=False):
    """Write a two-chunk document at tmp_path/rel and ingest it with `rel`
    as its identity, the way the refresh does."""
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("# Doc\n\nlead\n\n## One\n\nbody\n")
    return ingest.ingest_file(
        store, owner.id, Path(rel), project=project, root=tmp_path,
        archive=archive,
    )


def test_anchors_are_the_entries_with_src_but_no_sec(store, owner, tmp_path):
    _ingest(store, owner, tmp_path, "docs/a.md")
    _ingest(store, owner, tmp_path, "docs/b.md", archive=True)

    found = store.anchors(owner.id, "proj")

    assert sorted(e.title for e in found) == ["Doc", "Doc"]
    assert {t for e in found for t in e.tags} == {"src:docs/a.md", "src:docs/b.md"}
    assert {e.origin for e in found} == {Origin.INGESTED, Origin.ARCHIVED}


def test_anchors_excludes_superseded_other_projects_and_other_owners(
    store, owner, other, tmp_path
):
    _ingest(store, owner, tmp_path, "docs/a.md")
    _ingest(store, owner, tmp_path, "docs/elsewhere.md", project="other-proj")
    _ingest(store, other, tmp_path, "docs/theirs.md")
    # A hand-written doc with a src-looking tag but no ingest origin.
    write.remember(store, owner.id, title="Hand", body="x", kind=Kind.DOC,
                   project="proj", tags=["src:docs/hand.md"])
    [anchor] = [e for e in store.anchors(owner.id, "proj")]
    theirs = store.anchors(other.id, "proj")

    replacement = write.remember(store, owner.id, title="Doc", body="new",
                                 kind=Kind.DOC, project="proj",
                                 tags=["src:docs/a.md"], origin=Origin.INGESTED)
    store.set_superseded(anchor.id, replacement.id, owner.id)

    after = store.anchors(owner.id, "proj")
    assert [e.id for e in after] == [replacement.id]
    assert [e.id for e in theirs] != [] and all(e.owner_id == other.id for e in theirs)
```

If `write.remember`'s keyword names differ (`grep -n "def remember" -A12 src/remem/services/write.py`), match them. If `Origin.INGESTED` is not accepted by `remember` for a non-extracted caller, use `ingest.ingest_file` again with new content instead of `remember` for the replacement, and take the new anchor from `store.anchors`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_store_ingest_runs.py -k anchors -v`
Expected: FAIL with `AttributeError: 'PostgresStore' object has no attribute 'anchors'`

- [ ] **Step 3: Add to the Protocol**

In `src/remem/store.py`, after `latest_ingest_run`:

```python
    #: Live ingested/archived entries in a project that carry a `src:` tag
    #: and no `sec:` tag - one per ingested document. `search` cannot say
    #: "has a tag with this prefix and lacks one with that prefix", and
    #: pulling every chunk through it to filter in Python meets Query.limit
    #: on any project with a few hundred chunks. Newest first.
    def anchors(self, owner_id: UUID, project: str) -> list[Entry]: ...
```

- [ ] **Step 4: Implement**

In `src/remem/backends/postgres/store.py`, after `latest_ingest_run`:

```python
    def anchors(self, owner_id: UUID, project: str) -> list[Entry]:
        # `%%` because this statement takes positional parameters, so a
        # literal percent has to be doubled for psycopg's formatter.
        with self._cur() as cur:
            cur.execute(
                f"""
                select {entry_columns("e")} from entries e
                 where e.owner_id = %s
                   and e.project = %s
                   and e.superseded_by is null
                   and e.origin in ('ingested', 'archived')
                   and exists (select 1 from unnest(e.tags) t where t like 'src:%%')
                   and not exists (select 1 from unnest(e.tags) t where t like 'sec:%%')
                 order by e.created_at desc
                """,
                (owner_id, project),
            )
            return [_row_to_entry(r) for r in cur.fetchall()]
```

Check the enum literal spellings against `Origin` in `domain.py` (`grep -n "INGESTED\|ARCHIVED" src/remem/domain.py`).

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_store_ingest_runs.py -v`
Expected: all PASS.

- [ ] **Step 6: Watch the owner filter fail**

Temporarily remove `e.owner_id = %s and` from the query (and drop `owner_id` from the params tuple), run `-k excludes`, confirm FAIL, restore.

- [ ] **Step 7: Commit**

```bash
git add src/remem/store.py src/remem/backends/postgres/store.py tests/test_store_ingest_runs.py
git commit -m "Add Store.anchors: one live entry per ingested document"
```

---

### Task 3: The twin warning

**Files:**
- Modify: `src/remem/services/ingest.py` (`Report`, `ingest_file`, new `find_twin`, new `_src_of`)
- Test: `tests/test_ingest_twins.py`

**Interfaces:**
- Consumes: `Store.anchors` (Task 2)
- Produces:
  - `Report.twins: list[tuple[str, str, int]]` - `(new path, existing path, live chunk count)`, merged by `Report.merge`
  - `ingest.find_twin(path: Path, anchors: Iterable[Entry]) -> tuple[str, Entry] | None`
  - `ingest._src_of(entry: Entry) -> str` - the `src:` path or `""`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ingest_twins.py
"""A file that comes in entirely new while an anchor with the same
filename already has live chunks under another src: path.

The two ordinary causes are a moved file and a document ingested twice
under two identities (an absolute path once, a relative one later). Both
print the same healthy-looking "N new" report without this. A twin is a
question for the user, never an automatic supersede.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from remem.domain import Entry, Kind, Origin
from remem.services import ingest


def _anchor(src: str) -> Entry:
    return Entry(
        id=uuid4(), kind=Kind.DOC, title="Doc", body="", project="proj",
        owner_id=uuid4(), tags=[f"src:{src}"], origin=Origin.INGESTED,
    )


# ---- pure: no marker, runs on CI ----


def test_same_filename_under_another_path_is_a_twin():
    twin = ingest.find_twin(Path("docs/a.md"), [_anchor("notes/docs/a.md")])
    assert twin is not None
    assert twin[0] == "notes/docs/a.md"


def test_the_files_own_path_is_not_its_twin():
    assert ingest.find_twin(Path("docs/a.md"), [_anchor("docs/a.md")]) is None


def test_same_stem_different_extension_is_not_a_twin():
    assert ingest.find_twin(Path("docs/a.md"), [_anchor("docs/a.txt")]) is None


def test_no_anchors_no_twin():
    assert ingest.find_twin(Path("docs/a.md"), []) is None


# ---- db: the check wired into ingest_file ----


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
def other(store):
    return store.ensure_principal("someone-else")


def _write(root: Path, rel: str) -> None:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("# Doc\n\nlead\n\n## One\n\nbody\n")


@pytest.mark.db
def test_a_new_file_with_a_twin_is_reported_with_its_live_count(store, owner, tmp_path):
    _write(tmp_path, "notes/docs/a.md")
    _write(tmp_path, "docs/a.md")
    ingest.ingest_file(store, owner.id, Path("notes/docs/a.md"),
                       project="proj", root=tmp_path)

    report = ingest.ingest_file(store, owner.id, Path("docs/a.md"),
                                project="proj", root=tmp_path)

    assert report.created == 2
    assert report.twins == [("docs/a.md", "notes/docs/a.md", 2)]


@pytest.mark.db
def test_a_re_ingest_of_an_existing_file_reports_no_twin(store, owner, tmp_path):
    _write(tmp_path, "docs/a.md")
    ingest.ingest_file(store, owner.id, Path("docs/a.md"), project="proj", root=tmp_path)

    report = ingest.ingest_file(store, owner.id, Path("docs/a.md"),
                                project="proj", root=tmp_path)

    assert report.unchanged == 2
    assert report.twins == []


@pytest.mark.db
def test_the_twin_check_never_sees_another_owners_chunks(store, owner, other, tmp_path):
    _write(tmp_path, "notes/docs/a.md")
    _write(tmp_path, "docs/a.md")
    ingest.ingest_file(store, other.id, Path("notes/docs/a.md"),
                       project="proj", root=tmp_path)

    report = ingest.ingest_file(store, owner.id, Path("docs/a.md"),
                                project="proj", root=tmp_path)

    assert report.twins == []


@pytest.mark.db
def test_a_dry_run_still_reports_the_twin(store, owner, tmp_path):
    _write(tmp_path, "notes/docs/a.md")
    _write(tmp_path, "docs/a.md")
    ingest.ingest_file(store, owner.id, Path("notes/docs/a.md"),
                       project="proj", root=tmp_path)

    report = ingest.ingest_file(store, owner.id, Path("docs/a.md"),
                                project="proj", root=tmp_path, dry_run=True)

    assert report.twins == [("docs/a.md", "notes/docs/a.md", 2)]
```

Check `Entry`'s constructor fields (`sed -n 94,120p src/remem/domain.py`) and adjust `_anchor` to pass every required positional field.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ingest_twins.py -v`
Expected: the four pure tests FAIL with `AttributeError: module ... has no attribute 'find_twin'`; the db tests FAIL the same way (or SKIP if Postgres is down - bring it up).

- [ ] **Step 3: Implement**

In `src/remem/services/ingest.py`:

Add `from collections.abc import Callable, Iterable` (replace the existing `Callable` import line).

Extend `Report`:

```python
@dataclass(slots=True)
class Report:
    created: int = 0
    changed: int = 0
    unchanged: int = 0
    swept: int = 0
    failures: list[tuple[Path, str]] = field(default_factory=list)
    #: (new path, existing src path, live chunks under it) - files that came
    #: in entirely new while an anchor with the same filename was already
    #: live under another src: tag. See `find_twin`. Advisory: the CLI
    #: prints them and exits 0, the refresh records them in its run row.
    twins: list[tuple[str, str, int]] = field(default_factory=list)

    def merge(self, other: Report) -> None:
        self.created += other.created
        self.changed += other.changed
        self.unchanged += other.unchanged
        self.swept += other.swept
        self.failures.extend(other.failures)
        self.twins.extend(other.twins)
```

After `_sec_of`, add:

```python
def _src_of(entry: Entry) -> str:
    """An entry's `src:` path, or "" when it has none."""
    for tag in entry.tags:
        if tag.startswith("src:"):
            return tag[len("src:"):]
    return ""


def find_twin(path: Path, anchors: Iterable[Entry]) -> tuple[str, Entry] | None:
    """An anchor whose src: path has the same filename as `path`, under a
    different path - or None.

    Same final component, not same stem: `a.md` and `a.txt` are two files.
    The file's own src: path is excluded because the caller only asks on
    the entirely-new branch, where it cannot be live - and if it somehow
    were, "you are your own twin" is not a useful warning.

    A moved file and a document ingested twice under two identities look
    identical from here, and the user knows which. So this returns a
    question, and nothing acts on the answer automatically.
    """
    own = Path(path).as_posix()
    name = PurePosixPath(own).name
    for anchor in anchors:
        src = _src_of(anchor)
        if src and src != own and PurePosixPath(src).name == name:
            return src, anchor
    return None
```

In `ingest_file`, after `existing = {...}` and `report = Report()` are built, and before the `for chunk in chunks:` loop, add:

```python
    if not existing and project is not None:
        # Entirely new to remem under this identity. That is what a first
        # ingest looks like, and also what a second identity for a document
        # already here looks like - a moved file, or an absolute path once
        # and a relative one now. The two print identical counts, so this is
        # the one moment the difference can be pointed at. Read-only, so it
        # runs on a dry run too. Skipped without a project: anchors are
        # keyed on one, and a --global ingest has none.
        twin = find_twin(path, store.anchors(owner_id, project))
        if twin is not None:
            src, _ = twin
            live = len(_live_chunks(store, owner_id, Path(src)))
            report.twins.append((path.as_posix(), src, live))
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_ingest_twins.py tests/test_ingest_service.py tests/test_ingest_sweep.py -v`
Expected: all PASS, 0 skipped.

- [ ] **Step 5: Watch the guard fail**

Temporarily change `src != own` to `True` in `find_twin`; `test_the_files_own_path_is_not_its_twin` must FAIL. Restore.

- [ ] **Step 6: Commit**

```bash
git add src/remem/services/ingest.py tests/test_ingest_twins.py
git commit -m "Warn when a new file has a live twin under another src: path"
```

---

### Task 4: Run rows from `refresh` and `ingest_manual`

**Files:**
- Modify: `src/remem/services/ingest.py` (`refresh`; new `ingest_manual`, `_outcome`)
- Modify: `src/remem/cli.py` (`_reingest_once` opens `_session(autocommit=True)`)
- Test: `tests/test_ingest_refresh.py` (append)

**Interfaces:**
- Consumes: `Store.start_ingest_run`, `Store.finish_ingest_run`, `Store.latest_ingest_run` (Task 1); `Report.twins` (Task 3)
- Produces:
  - `ingest.ingest_manual(store, owner_id, paths: list[Path], *, project: str | None, root: Path | None = None, archive: bool = False, dry_run: bool = False) -> Report` - `ingest_paths` bracketed by a `MANUAL` run row; no row for a dry run or when `project is None`
  - `refresh` unchanged in signature; writes an `AUTO` row

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ingest_refresh.py`:

```python
from remem.domain import IngestTrigger


def test_refresh_records_a_finished_run_row(store, owner, root):
    ingest.designate(store, owner.id, "proj", ["docs/specs"])
    ingest.designate(store, owner.id, "proj", ["docs/plans"], archive=True)

    ingest.refresh(store, owner.id, "proj", root,
                   embed_model="fake-2", load_embedder=FakeEmbedder)

    run = store.latest_ingest_run(owner.id, "proj")
    assert run is not None
    assert run.trigger is IngestTrigger.AUTO
    assert run.finished_at is not None
    assert run.created == 3          # two chunks from one.md, one from two.md
    assert run.embedded == 3
    assert run.failures == []
    assert run.embed_error is None


def test_an_undesignated_project_writes_no_run_row(store, owner, root):
    ingest.refresh(store, owner.id, "proj", root,
                   embed_model="fake-2", load_embedder=FakeEmbedder)

    assert store.latest_ingest_run(owner.id, "proj") is None


def test_a_missing_designated_path_lands_in_the_rows_failures(store, owner, root):
    ingest.designate(store, owner.id, "proj", ["docs/specs", "docs/renamed"])

    ingest.refresh(store, owner.id, "proj", root,
                   embed_model="fake-2", load_embedder=FakeEmbedder)

    run = store.latest_ingest_run(owner.id, "proj")
    assert run.created == 2
    assert [f["path"] for f in run.failures] == ["docs/renamed"]
    assert "No such file" in run.failures[0]["reason"]


def test_an_absent_embedder_is_recorded_not_raised(store, owner, root):
    ingest.designate(store, owner.id, "proj", ["docs/specs"])

    def broken():
        raise EmbedderUnavailable("fastembed is not installed")

    ingest.refresh(store, owner.id, "proj", root,
                   embed_model="fake-2", load_embedder=broken)

    run = store.latest_ingest_run(owner.id, "proj")
    assert run.finished_at is not None
    assert run.created == 2
    assert run.embed_error == "fastembed is not installed"


def test_an_exception_mid_run_is_recorded_and_re_raised(store, owner, root, monkeypatch):
    ingest.designate(store, owner.id, "proj", ["docs/specs"])

    def explode(*a, **kw):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(ingest, "ingest_paths", explode)

    with pytest.raises(RuntimeError):
        ingest.refresh(store, owner.id, "proj", root,
                       embed_model="fake-2", load_embedder=FakeEmbedder)

    run = store.latest_ingest_run(owner.id, "proj")
    assert run.finished_at is not None
    assert run.failures == [{"path": "*", "reason": "RuntimeError: disk on fire"}]


def test_ingest_manual_records_a_manual_row(store, owner, root):
    report = ingest.ingest_manual(
        store, owner.id, [Path("docs/specs")], project="proj", root=root,
    )

    run = store.latest_ingest_run(owner.id, "proj")
    assert report.created == 2
    assert run.trigger is IngestTrigger.MANUAL
    assert run.created == 2
    assert run.embedded == 0


def test_ingest_manual_writes_no_row_for_a_dry_run_or_no_project(store, owner, root):
    ingest.ingest_manual(store, owner.id, [Path("docs/specs")],
                         project="proj", root=root, dry_run=True)
    ingest.ingest_manual(store, owner.id, [Path("docs/specs")],
                         project=None, root=root)

    assert store.latest_ingest_run(owner.id, "proj") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ingest_refresh.py -v`
Expected: the new tests FAIL (`latest_ingest_run` returns None; `ingest_manual` missing). The existing ones still pass.

- [ ] **Step 3: Implement**

In `src/remem/services/ingest.py`, add `IngestTrigger` to the `from remem.domain import (...)` line. Replace `refresh` and add the helpers:

```python
def _outcome(report: Report, *, embedded: int, embed_error: str | None) -> dict:
    """A report as `Store.finish_ingest_run` keyword arguments.

    Paths become strings here, once, so the two callers that write a row
    cannot disagree about the stored shape.
    """
    return dict(
        created=report.created, changed=report.changed,
        unchanged=report.unchanged, swept=report.swept, embedded=embedded,
        failures=[{"path": Path(p).as_posix(), "reason": r}
                  for p, r in report.failures],
        twins=[{"path": p, "existing": e, "live": n}
               for p, e, n in report.twins],
        embed_error=embed_error,
    )


def ingest_manual(
    store: Store,
    owner_id: UUID,
    paths: list[Path],
    *,
    project: str | None,
    root: Path | None = None,
    archive: bool = False,
    dry_run: bool = False,
) -> Report:
    """`ingest_paths`, bracketed by a run row - what `remem ingest` calls.

    The row is what lets "when was this project last ingested at all" have
    one answer whether a person or the refresh did it. No row for a dry run
    (nothing happened) or without a project (the row is keyed on one, and
    a --global ingest has none). `ingest_paths` itself stays row-free
    because `refresh` calls it once per designation half and wraps the
    whole loop in a single row.

    Fail-loud like its caller: a failure to write the row is an error like
    any other, and the single transaction rolls the entries back with it.
    """
    if dry_run or project is None:
        return ingest_paths(store, owner_id, paths, project=project,
                            root=root, archive=archive, dry_run=dry_run)
    run = store.start_ingest_run(owner_id, project, IngestTrigger.MANUAL,
                                 archive=archive)
    report = ingest_paths(store, owner_id, paths, project=project,
                          root=root, archive=archive)
    store.finish_ingest_run(
        run.id, owner_id, **_outcome(report, embedded=0, embed_error=None)
    )
    return report


def refresh(
    store: Store,
    owner_id: UUID,
    project: str,
    root: Path,
    *,
    embed_model: str,
    load_embedder: Callable[[], Embedder],
) -> RefreshResult:
    """Re-ingest this project's designated paths, then embed what is missing.

    The policy behind the detached job a session start spawns. An
    undesignated project does nothing at all - that silence is the opt-in,
    and it is also the common case, so it must cost nothing: no file is
    read, no embedder is built, and no run row is written.

    Paths are resolved against `root` (the git root) rather than the process
    working directory. The spawned job inherits whatever directory the
    harness happened to be in, which is not something a designation made
    weeks earlier can know.

    Everything this learns goes into a run row (017_ingest_runs.sql),
    because the caller is a detached process whose stderr is /dev/null: the
    row is the only record on the machine that this ran. It is started
    BEFORE any file is read so that a process which dies mid-run leaves a
    started, unfinished row - "crashed" rather than "never ran". That only
    holds if the started row is committed first, which is why `reingest
    run` opens its session with autocommit.

    A Python exception is a third case: recorded as a failure with path
    `*`, the row finished, and the exception re-raised for the caller's
    guard. The reason is kept in the row rather than lost to a debug
    channel nobody reads. A path that vanished lands in `report.failures`
    and an absent embedder in `embed_error`; neither raises.
    """
    result = RefreshResult()
    designated = store.ingest_designations(owner_id, project)
    if not designated:
        return result

    run = store.start_ingest_run(owner_id, project, IngestTrigger.AUTO)
    try:
        for designation in designated:
            result.report.merge(
                ingest_paths(
                    store, owner_id,
                    [Path(p) for p in designation.paths],
                    project=project,
                    root=root,
                    archive=designation.archive,
                )
            )

        try:
            embedded = backfill_if_pending(
                store, owner_id, embed_model, load_embedder
            )
        except Exception as exc:
            # Losing the semantic tier is worth strictly less than the
            # entries just written, and `remem embed` remains the loud way
            # to find out that the embedder is broken.
            result.embed_error = str(exc)
        else:
            result.embedded = embedded.embedded if embedded else 0
    except BaseException as exc:
        # BaseException, matching the guard in `reingest run`: a
        # `typer.Exit` from a nested helper is a SystemExit, and a row that
        # says nothing about why it stopped is the gap this table closes.
        result.report.failures.append(
            (Path("*"), f"{type(exc).__name__}: {exc}")
        )
        store.finish_ingest_run(
            run.id, owner_id,
            **_outcome(result.report, embedded=result.embedded,
                       embed_error=result.embed_error),
        )
        raise

    store.finish_ingest_run(
        run.id, owner_id,
        **_outcome(result.report, embedded=result.embedded,
                   embed_error=result.embed_error),
    )
    return result
```

In `src/remem/cli.py`, in `_reingest_once`, change `with _session() as s:` to:

```python
    # autocommit, like `remem events process`: this is long-running work
    # that records its own progress. The run row is started before any file
    # is read, and it has to be COMMITTED then, or a process that dies
    # mid-run rolls its own "I started" back and looks like it never ran.
    with _session(autocommit=True) as s:
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_ingest_refresh.py tests/test_reingest_cli.py tests/test_hook_spawn_ingest.py -v`
Expected: all PASS, 0 skipped.

- [ ] **Step 5: Watch the guard fail**

Temporarily replace the `except BaseException` block with a bare re-raise (delete the `failures.append` and `finish_ingest_run` lines inside it), run `-k exception_mid_run`, confirm FAIL, restore.

- [ ] **Step 6: Commit**

```bash
git add src/remem/services/ingest.py src/remem/cli.py tests/test_ingest_refresh.py
git commit -m "Record every ingest run in ingest_runs"
```

---

### Task 5: Repository-relative identity for `remem ingest`

**Files:**
- Modify: `src/remem/project.py` (new `toplevel`)
- Modify: `src/remem/services/ingest.py` (new `relative_to_root`)
- Modify: `src/remem/cli.py` (`ingest` command, line ~275)
- Modify: `tests/test_ingest_cli.py` (`docs` fixture chdirs; new tests)
- Test: `tests/test_ingest_identity.py`

**Interfaces:**
- Consumes: `ingest_manual` (Task 4), `Report.twins` (Task 3)
- Produces:
  - `project.toplevel(start: Path | None = None) -> Path | None` - the working tree's top level from `git rev-parse --show-toplevel`, resolved; `None` outside a repository
  - `ingest.relative_to_root(paths: list[Path], top: Path, cwd: Path) -> list[Path]` - raises `BadDesignation` for a path outside `top`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ingest_identity.py
"""Chunk identity is the repository-relative path, computed by the CLI.

`remem ingest` used to identify a chunk by the path as typed, so `cd docs
&& remem ingest a.md` stored `src:a.md` where a root run stored
`src:docs/a.md` and duplicated every chunk. A rule said to run from the
root; a rule is not a fix. No db marker: these use a scratch git repo
and no store.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from remem.project import toplevel
from remem.services import ingest


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True,
                   capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "myrepo"
    (root / "docs" / "deep").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "README.md").write_text("hi\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "init")
    return root.resolve()


def test_toplevel_is_the_working_tree_root_from_a_subdirectory(repo):
    assert toplevel(repo / "docs" / "deep") == repo


def test_toplevel_of_a_worktree_is_the_worktree_not_the_main_checkout(repo, tmp_path):
    wt = tmp_path / "somewhere-else"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "wt-branch")
    assert toplevel(wt) == wt.resolve()


def test_toplevel_is_none_outside_a_repository(tmp_path):
    outside = tmp_path / "plain"
    outside.mkdir()
    assert toplevel(outside) is None


def test_relative_from_the_root_is_unchanged(repo):
    assert ingest.relative_to_root([Path("docs/a.md")], repo, cwd=repo) == [Path("docs/a.md")]


def test_relative_from_a_subdirectory_is_rewritten(repo):
    assert ingest.relative_to_root([Path("a.md")], repo, cwd=repo / "docs") == [Path("docs/a.md")]


def test_absolute_inside_the_repository_is_rewritten(repo):
    assert ingest.relative_to_root([repo / "docs" / "a.md"], repo, cwd=repo / "docs" / "deep") == [Path("docs/a.md")]


def test_a_path_outside_the_repository_is_refused(repo, tmp_path):
    with pytest.raises(ingest.BadDesignation, match="outside the repository"):
        ingest.relative_to_root([tmp_path / "elsewhere.md"], repo, cwd=repo)


def test_a_dot_dot_that_stays_inside_is_fine(repo):
    assert ingest.relative_to_root([Path("../docs/a.md")], repo, cwd=repo / "docs") == [Path("docs/a.md")]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ingest_identity.py -v`
Expected: FAIL with `ImportError: cannot import name 'toplevel'`.

- [ ] **Step 3: Implement `toplevel` and `relative_to_root`**

In `src/remem/project.py`, after `repo_root`:

```python
def toplevel(start: Path | None = None) -> Path | None:
    """The WORKING TREE's top level, or None outside a repository.

    Not `repo_root`, which resolves a worktree to the main checkout: a
    file inside a worktree is not under the main checkout's root, so a
    path relative to it cannot be computed. The relative path is the same
    against either, which is what makes a worktree ingest and a
    main-checkout ingest of the same file the same identity - the same
    property `resolve_project` gives the project name. Resolved, so a
    caller can `relative_to` it against a resolved path (macOS /tmp is a
    symlink to /private/tmp, and `relative_to` is textual).
    """
    start = (start or Path.cwd()).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return Path(result.stdout.strip()).resolve()
    except OSError:
        return None
```

In `src/remem/services/ingest.py`, after `designations`:

```python
def relative_to_root(paths: list[Path], top: Path, cwd: Path) -> list[Path]:
    """Each path as the repository-relative identity `ingest_file` stores.

    Inside a repository, a chunk's `src:` tag is its path from the working
    tree's top level - the same identity the automatic refresh computes
    against the git root - regardless of where the command was typed or
    whether the argument was absolute. Before this, the tag was the path
    as typed, and `cd docs && remem ingest a.md` duplicated every chunk a
    root run had stored under `docs/a.md`.

    A path outside the repository is refused, loudly, because it has no
    root-relative identity and inventing one (the absolute path, say) is
    the duplication this exists to end. Same class as `designate`'s
    refusals, for the same reason: this is the moment there is a human to
    tell.

    Pure - takes `cwd` rather than reading it - so it is testable without
    changing directory.
    """
    top = top.resolve()
    out: list[Path] = []
    for raw in paths:
        located = (cwd / raw).resolve()
        try:
            out.append(located.relative_to(top))
        except ValueError:
            raise BadDesignation(
                f"{raw!s} is outside the repository at {top}. Inside a "
                f"repository, ingest identifies documents by their "
                f"repository-relative path; to ingest a file elsewhere, run "
                f"from outside any repository or pass --project."
            ) from None
    return out
```

- [ ] **Step 4: Run the identity tests**

Run: `uv run pytest tests/test_ingest_identity.py -v`
Expected: all PASS.

- [ ] **Step 5: Write the failing CLI tests**

In `tests/test_ingest_cli.py`, change the `docs` fixture so the CLI runs outside any repository - the test process's cwd is the remem checkout, and a tmp_path argument from inside it is now refused:

```python
@pytest.fixture
def docs(tmp_path, monkeypatch):
    # The chunk title comes from the h1, not the filename stem, so this file
    # produces "Alpha doc" and "Alpha doc § One".
    #
    # chdir: the test process runs from the remem checkout, and inside a
    # repository `remem ingest` refuses a path outside it. tmp_path is not
    # under any repository, so from here identity is the path as typed.
    (tmp_path / "alpha-doc.md").write_text("# Alpha doc\n\nlead\n\n## One\n\nbody one\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path
```

Append:

```python
import subprocess


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real repository, because identity resolves against its top level."""
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.md").write_text("# A\n\nlead\n\n## One\n\nbody one\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    monkeypatch.chdir(root)
    return root


def test_ingest_from_a_subdirectory_supersedes_rather_than_duplicates(env, repo, monkeypatch):
    first = runner.invoke(app, ["ingest", "docs/a.md"])
    assert first.exit_code == 0, first.output
    assert "2 new" in first.stdout

    (repo / "docs" / "a.md").write_text("# A\n\nlead\n\n## One\n\nbody two\n")
    monkeypatch.chdir(repo / "docs")
    second = runner.invoke(app, ["ingest", "a.md"])

    assert second.exit_code == 0, second.output
    assert "0 new, 1 changed, 1 unchanged" in second.stdout


def test_ingest_refuses_a_path_outside_the_repository(env, repo, tmp_path):
    outside = tmp_path / "elsewhere.md"
    outside.write_text("# E\n\nbody\n")

    result = runner.invoke(app, ["ingest", str(outside)])

    assert result.exit_code == 1
    assert "outside the repository" in result.output


def test_ingest_records_a_manual_run_row(env, repo):
    runner.invoke(app, ["ingest", "docs/a.md"])

    result = runner.invoke(app, ["reingest", "status"])
    assert "last run: manual" in result.stdout


def test_ingest_prints_a_twin_and_still_exits_zero(env, repo):
    (repo / "notes").mkdir()
    (repo / "notes" / "a.md").write_text("# A\n\nlead\n\n## One\n\nbody one\n")
    runner.invoke(app, ["ingest", "notes/a.md"])

    result = runner.invoke(app, ["ingest", "docs/a.md"])

    assert result.exit_code == 0
    assert "twin: docs/a.md is new, but src:notes/a.md has 2 live chunks" in result.output
```

`test_ingest_records_a_manual_run_row` depends on Task 7's rendering. Mark it `@pytest.mark.xfail(strict=True, reason="rendered by reingest status in Task 7")` for now; Task 7 removes the marker.

- [ ] **Step 6: Run to verify failure**

Run: `uv run pytest tests/test_ingest_cli.py -v`
Expected: the existing tests PASS (the chdir keeps them outside a repo); the subdirectory test FAILS on `"0 new, 1 changed"` (it reports `2 new`); the refusal test FAILS on exit code; the twin test FAILS on the missing line.

- [ ] **Step 7: Change the `ingest` command**

In `src/remem/cli.py`, add `toplevel` to the `from remem.project import ...` line. Replace the body of `ingest`:

```python
    resolved = _resolve_project(project, is_global)
    given = list(paths)
    root = None
    top = toplevel()
    if top is not None:
        # Inside a repository, identity is the repository-relative path -
        # the same one the automatic refresh computes - whatever directory
        # this was typed from. Outside one, it stays the path as typed.
        try:
            given = ingest_service.relative_to_root(given, top, cwd=Path.cwd())
        except ingest_service.BadDesignation as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        root = top
    with _session() as s:
        report = ingest_service.ingest_manual(
            s.store, s.owner.id, given,
            project=resolved, root=root, archive=archive, dry_run=dry_run,
        )
    prefix = "Would write: " if dry_run else ""
    typer.echo(
        f"{prefix}{report.created} new, {report.changed} changed, "
        f"{report.unchanged} unchanged, {report.swept} swept."
    )
    for path, existing, live in report.twins:
        # A question, not a failure - a moved file and a document ingested
        # under two identities look the same from here. Exit code unchanged.
        typer.echo(
            f"twin: {path} is new, but src:{existing} has {live} live "
            f"chunks - a moved file, or ingested from a different directory?",
            err=True,
        )
    for path, reason in report.failures:
        typer.echo(f"failed: {path}: {reason}", err=True)
    if report.failures:
        # Fail-loud, unlike every hook in this repo: a person typed this.
        raise typer.Exit(1)
    if not dry_run and (report.created or report.changed):
        # Not after a dry run: nothing was written, so there is nothing to
        # embed, and saying otherwise sends the user to a no-op.
        typer.echo("Run `remem embed` to give the new entries vectors.")
```

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/test_ingest_cli.py tests/test_ingest_identity.py tests/test_mcp_ingest.py -v`
Expected: all PASS except the xfail, 0 skipped. If `test_mcp_ingest.py` breaks, read `grep -n "ingest" src/remem/mcp_server.py`: the MCP tool should keep calling `ingest_paths` unchanged; only fix it if this task changed a name it uses.

- [ ] **Step 9: Commit**

```bash
git add src/remem/project.py src/remem/services/ingest.py src/remem/cli.py tests/test_ingest_identity.py tests/test_ingest_cli.py
git commit -m "Identify ingested chunks by repository-relative path"
```

---

### Task 6: `status`, `render_status`, `status_to_dict`, `advisories`

**Files:**
- Modify: `src/remem/services/ingest.py` (append)
- Test: `tests/test_ingest_status.py`

**Interfaces:**
- Consumes: `IngestRun` (Task 1), `Store.latest_ingest_run`, `Store.ingest_designations`
- Produces:
  - `ingest.ProjectIngestStatus` dataclass: `project: str`, `designations: list[IngestDesignation]`, `last_run: IngestRun | None`, `checked_against: Path | None`, `missing: list[str]`
  - `ingest.status(store, owner_id, project: str | None, *, current_project: str | None, root: Path | None) -> list[ProjectIngestStatus]`
  - `ingest.render_status(found: list[ProjectIngestStatus], project: str | None) -> str`
  - `ingest.status_to_dict(found: list[ProjectIngestStatus]) -> list[dict]`
  - `ingest.advisories(store, owner_id, *, current_project: str | None, root: Path | None) -> list[str]`
  - `ingest.STATUS_POINTER = "see: remem reingest status --project {project}"`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ingest_status.py
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
    base = dict(id=uuid4(), owner_id=uuid4(), project="remem",
                trigger=IngestTrigger.AUTO, started_at=AT, finished_at=AT)
    base.update(kw)
    return IngestRun(**base)


def _status(**kw) -> ingest.ProjectIngestStatus:
    base = dict(
        project="remem",
        designations=[IngestDesignation("remem", ("docs/specs", "docs/notes"))],
        last_run=None, checked_against=None, missing=[],
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
        [_status(last_run=_run(created=3, changed=1, unchanged=40, swept=0, embedded=4))],
        "remem",
    )
    assert f"last run: auto, {SHOWN}, 3 new, 1 changed, 40 unchanged, 0 swept, 4 embedded" in out
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
        [_status(checked_against=Path("/repo"), missing=["docs/notes"])], "remem",
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


def test_status_to_dict_carries_the_run_and_the_check():
    [d] = ingest.status_to_dict(
        [_status(last_run=_run(created=1), checked_against=Path("/repo"), missing=["x"])]
    )
    assert d["project"] == "remem"
    assert d["designations"] == [{"archive": False, "paths": ["docs/specs", "docs/notes"]}]
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

    found = {s.project: s for s in ingest.status(
        store, owner.id, None, current_project="here", root=root)}

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
            run.id, owner.id, created=0, changed=0, unchanged=0, swept=0,
            embedded=0, twins=[], embed_error=None,
            failures=[] if project == "clean" else [{"path": "x", "reason": "boom"}],
        )
    store.start_ingest_run(owner.id, "stuck", IngestTrigger.AUTO)

    lines = ingest.advisories(store, owner.id, current_project="renamed", root=root)

    assert len(lines) == 3
    assert any(l.startswith("failed:") and "1 failure(s)" in l
               and l.endswith("see: remem reingest status --project failed") for l in lines)
    assert any(l.startswith("stuck:") and "did not finish" in l for l in lines)
    assert any(l.startswith("renamed:") and "docs/gone" in l for l in lines)
    assert not any(l.startswith("clean:") for l in lines)


@pytest.mark.db
def test_advisories_are_empty_with_nothing_designated(store, owner, root):
    assert ingest.advisories(store, owner.id, current_project=None, root=None) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ingest_status.py -v`
Expected: FAIL with `AttributeError: ... 'ProjectIngestStatus'`.

- [ ] **Step 3: Implement**

Append to `src/remem/services/ingest.py` (add `IngestRun` to the domain import):

```python
# ---------------- status ----------------

#: Every advisory ends with this, scope and all. A pointer that leads to a
#: screen that contradicts the line teaches the user the line lies.
STATUS_POINTER = "see: remem reingest status --project {project}"


@dataclass(slots=True)
class ProjectIngestStatus:
    """What `remem reingest status` knows about one project.

    `checked_against` is None when the designated paths were NOT checked
    on disk - the project shown is not the one the current directory
    resolves to, so there is no root to check against. `missing` is then
    empty by construction, and the renderer says "not checked" rather
    than letting an empty list read as "all present".
    """

    project: str
    designations: list[IngestDesignation]
    last_run: IngestRun | None
    checked_against: Path | None
    missing: list[str]


def status(
    store: Store,
    owner_id: UUID,
    project: str | None,
    *,
    current_project: str | None,
    root: Path | None,
) -> list[ProjectIngestStatus]:
    """The facts behind `remem reingest status`. Read-only.

    `project=None` lists every designated project, as the command always
    has. The on-disk check runs only for `current_project`, against
    `root`: designations belong to a project and store no working
    directory (016_ingest_designations.sql), so the root is only known
    for the project this process is running inside. Every other project
    gets `checked_against=None`, which renders as "not checked" - a
    negative verdict has to name the ground it covered.
    """
    by_project: dict[str, list[IngestDesignation]] = {}
    for d in store.ingest_designations(owner_id, project):
        by_project.setdefault(d.project, []).append(d)

    found: list[ProjectIngestStatus] = []
    for name, designations in by_project.items():
        checked_against = None
        missing: list[str] = []
        if root is not None and name == current_project:
            checked_against = root
            for d in designations:
                for p in d.paths:
                    if not _exists(root / p):
                        missing.append(p)
        found.append(ProjectIngestStatus(
            project=name,
            designations=designations,
            last_run=store.latest_ingest_run(owner_id, name),
            checked_against=checked_against,
            missing=missing,
        ))
    return found


def _exists(path: Path) -> bool:
    """`Path.exists`, with an unreadable path reading as missing rather
    than as a crash - this is a status command."""
    try:
        return path.exists()
    except OSError:
        return False


def _when(at: datetime | None) -> str:
    return at.astimezone().strftime("%Y-%m-%d %H:%M") if at else "?"


def _run_lines(run: IngestRun | None) -> list[str]:
    """The last-run block. Four states, four spellings - each calls for a
    different action, so none may be mistaken for another."""
    if run is None:
        return ["last run: never. A session start inside this repository "
                "spawns one."]
    if run.finished_at is None:
        return [f"last run: {run.trigger}, started {_when(run.started_at)}, "
                f"did not finish"]
    lines = [
        f"last run: {run.trigger}, {_when(run.started_at)}, "
        f"{run.created} new, {run.changed} changed, {run.unchanged} unchanged, "
        f"{run.swept} swept, {run.embedded} embedded"
    ]
    for f in run.failures:
        lines.append(f"  failed: {f['path']}: {f['reason']}")
    for t in run.twins:
        lines.append(
            f"  twin: {t['path']} is new, but src:{t['existing']} has "
            f"{t['live']} live chunks"
        )
    if run.embed_error:
        lines.append(f"  embed skipped: {run.embed_error}")
    return lines


def render_status(found: list[ProjectIngestStatus], project: str | None) -> str:
    """Human-readable `remem reingest status`. `status_to_dict` is the
    `--json` half; both read the same objects so they cannot disagree."""
    if not found:
        return (
            f"{project or 'This project'} is not designated for automatic "
            f"re-ingest. Designate it with `remem reingest designate "
            f"<paths>`."
        )
    lines: list[str] = []
    for s in found:
        for d in s.designations:
            half = "archive" if d.archive else "default"
            lines.append(f"{s.project} ({half}): {', '.join(d.paths)}")
        lines.extend(_run_lines(s.last_run))
        if s.checked_against is None:
            lines.append(
                f"paths not checked: run from inside {s.project}'s repository"
            )
        elif s.missing:
            lines.append(
                f"missing on disk: {', '.join(s.missing)}  "
                f"(checked against {s.checked_against})"
            )
        else:
            lines.append(
                f"all designated paths present  "
                f"(checked against {s.checked_against})"
            )
    return "\n".join(lines)


def _run_to_dict(run: IngestRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "id": str(run.id),
        "trigger": str(run.trigger),
        "archive": run.archive,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created": run.created, "changed": run.changed,
        "unchanged": run.unchanged, "swept": run.swept,
        "embedded": run.embedded,
        "failures": list(run.failures),
        "twins": list(run.twins),
        "embed_error": run.embed_error,
    }


def status_to_dict(found: list[ProjectIngestStatus]) -> list[dict]:
    return [
        {
            "project": s.project,
            "designations": [
                {"archive": d.archive, "paths": list(d.paths)}
                for d in s.designations
            ],
            "last_run": _run_to_dict(s.last_run),
            "check": {
                "checked_against": (
                    str(s.checked_against) if s.checked_against else None
                ),
                "missing": list(s.missing),
            },
        }
        for s in found
    ]


def advisories(
    store: Store,
    owner_id: UUID,
    *,
    current_project: str | None,
    root: Path | None,
) -> list[str]:
    """One line per designated project whose last run is unhealthy, for
    `remem record status` - the fail-loud half of a fail-soft pipeline,
    which already carries the doctor advisory the same way.

    Unhealthy is: the latest run finished with failures, or started and
    never finished, or (for the current project only, the one with a root
    to check against) a designated path is missing on disk. Every line
    ends with STATUS_POINTER, scope included.
    """
    lines: list[str] = []
    for s in status(store, owner_id, None, current_project=current_project,
                    root=root):
        pointer = STATUS_POINTER.format(project=s.project)
        run = s.last_run
        if run is not None and run.finished_at is None:
            lines.append(
                f"{s.project}: {run.trigger} ingest started "
                f"{_when(run.started_at)} and did not finish - {pointer}"
            )
        elif run is not None and run.failures:
            lines.append(
                f"{s.project}: last {run.trigger} ingest "
                f"({_when(run.started_at)}) had {len(run.failures)} "
                f"failure(s) - {pointer}"
            )
        if s.missing:
            lines.append(
                f"{s.project}: designated path(s) missing on disk: "
                f"{', '.join(s.missing)} - {pointer}"
            )
    return lines
```

Add `from datetime import datetime` to the imports at the top of `ingest.py`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_ingest_status.py -v`
Expected: all PASS, 0 skipped.

- [ ] **Step 5: Watch a guard fail**

Temporarily change `name == current_project` to `True` in `status`; `test_status_checks_disk_only_for_the_current_project` must FAIL. Restore.

- [ ] **Step 6: Commit**

```bash
git add src/remem/services/ingest.py tests/test_ingest_status.py
git commit -m "Ingest status and advisories from the run rows"
```

---

### Task 7: The command surface - `reingest status --json` and the `record status` advisory

**Files:**
- Modify: `src/remem/cli.py` (`reingest_status`, `record_status`)
- Modify: `src/remem/services/events.py` (`StatusReport`, `status`, `render`, `to_dict`)
- Modify: `tests/test_reingest_cli.py` (append), `tests/test_record_status.py` (append), `tests/test_ingest_cli.py` (drop the xfail)

**Interfaces:**
- Consumes: `ingest.status`, `render_status`, `status_to_dict`, `advisories` (Task 6)
- Produces: `events.StatusReport.ingest_advisories: list[str]`; `events.status(..., ingest_advisories: list[str] | None = None)`; `to_dict()["ingest_advisories"]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reingest_cli.py`:

```python
import json


def test_status_shows_the_last_run_after_a_run(env, repo):
    runner.invoke(app, ["reingest", "designate", "docs/specs"])
    runner.invoke(app, ["reingest", "run"])

    result = runner.invoke(app, ["reingest", "status"])

    assert result.exit_code == 0
    assert "last run: auto" in result.stdout
    assert "1 new" in result.stdout
    assert "all designated paths present" in result.stdout


def test_status_reports_a_designated_path_missing_on_disk(env, repo):
    runner.invoke(app, ["reingest", "designate", "docs/specs", "docs/gone"])

    result = runner.invoke(app, ["reingest", "status"])

    assert "missing on disk: docs/gone" in result.stdout
    assert f"checked against {repo.resolve()}" in result.stdout


def test_status_for_another_project_says_paths_were_not_checked(env, repo):
    runner.invoke(app, ["reingest", "designate", "docs/specs", "--project", "elsewhere"])

    result = runner.invoke(app, ["reingest", "status", "--project", "elsewhere"])

    assert "paths not checked: run from inside elsewhere's repository" in result.stdout


def test_status_json(env, repo):
    runner.invoke(app, ["reingest", "designate", "docs/specs"])
    runner.invoke(app, ["reingest", "run"])

    result = runner.invoke(app, ["reingest", "status", "--json"])

    [entry] = json.loads(result.stdout)
    assert entry["project"] == "repo"
    assert entry["last_run"]["trigger"] == "auto"
    assert entry["check"]["missing"] == []
```

`repo.resolve()` matters on macOS: `tmp_path` lives under `/private/var`, and `repo_root()` resolves.

Append to `tests/test_record_status.py`:

```python
from remem.domain import IngestTrigger
from remem.services import ingest


def test_status_carries_an_ingest_advisory(store, owner):
    ingest.designate(store, owner.id, "remem", ["docs/specs"])
    run = store.start_ingest_run(owner.id, "remem", IngestTrigger.AUTO)
    store.finish_ingest_run(
        run.id, owner.id, created=0, changed=0, unchanged=0, swept=0,
        embedded=0, twins=[], embed_error=None,
        failures=[{"path": "docs/specs", "reason": "gone"}],
    )
    lines = ingest.advisories(store, owner.id, current_project=None, root=None)

    report = events.status(store, owner.id, idle_seconds=IDLE,
                           ingest_advisories=lines)

    assert "! remem: last auto ingest" in events.render(report)
    assert events.to_dict(report)["ingest_advisories"] == lines
```

Check how the existing tests in that file build a `StatusReport` (`grep -n "events.status(" tests/test_record_status.py | head -3`) and match the call shape.

In `tests/test_ingest_cli.py`, remove the `xfail` marker from `test_ingest_records_a_manual_run_row`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_reingest_cli.py tests/test_record_status.py tests/test_ingest_cli.py -v`
Expected: the new tests FAIL (`--json` is not an option; `ingest_advisories` is not a keyword; `last run:` absent).

- [ ] **Step 3: Extend `events.StatusReport`**

In `src/remem/services/events.py`:

After `hook_advisories` in `StatusReport`:

```python
    #: Lines from `services/ingest.advisories`, passed in for the same
    #: reason `hook_advisories` is: this module is about events, and an
    #: ingest status that cannot be computed must not take down the
    #: events status.
    ingest_advisories: list[str] = field(default_factory=list)
```

`status()` gains `ingest_advisories: list[str] | None = None` after `hook_advisories` and passes `ingest_advisories=list(ingest_advisories or [])` into the `StatusReport`.

In `render`, after the `hook_advisories` loop:

```python
    for line in report.ingest_advisories:
        lines.append(f"! {line}")
```

In `to_dict`, after `"hook_advisories"`:

```python
        "ingest_advisories": list(report.ingest_advisories),
```

- [ ] **Step 4: Change the commands**

In `src/remem/cli.py`, replace `reingest_status`:

```python
@reingest_app.command("status")
def reingest_status(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Show what this project re-ingests automatically, and how the last
    run went.

    Designated paths are checked on disk only for the project this
    directory resolves to - it is the only one with a known root - and the
    report says so for any other, rather than letting silence read as
    "all present".
    """
    resolved = _resolve_project(project, False)
    current = resolve_project()
    with _session() as s:
        found = ingest_service.status(
            s.store, s.owner.id, resolved,
            current_project=current, root=repo_root(),
        )
    if as_json:
        typer.echo(json.dumps(ingest_service.status_to_dict(found), indent=2))
        return
    typer.echo(ingest_service.render_status(found, resolved))
```

In `record_status`, inside the `with _session() as s:` block, before `report = events.status(...)`:

```python
        # Wrapped like the doctor call above, and for the same reason: an
        # ingest status that cannot be computed must not take down the
        # events status it decorates.
        try:
            ingest_advisories = ingest_service.advisories(
                s.store, s.owner.id,
                current_project=resolve_project(), root=repo_root(),
            )
        except Exception:
            ingest_advisories = []
```

and pass `ingest_advisories=ingest_advisories` to `events.status`.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_reingest_cli.py tests/test_record_status.py tests/test_ingest_cli.py tests/test_ingest_status.py -v`
Expected: all PASS, 0 skipped, no xfail remaining.

- [ ] **Step 6: Commit**

```bash
git add src/remem/cli.py src/remem/services/events.py tests/test_reingest_cli.py tests/test_record_status.py tests/test_ingest_cli.py
git commit -m "Surface ingest runs in reingest status and record status"
```

---

### Task 8: CLAUDE.md, full suite, and a live check

**Files:**
- Modify: `CLAUDE.md` (under "### Ingested documents", after the `reingest` bullets)

- [ ] **Step 1: Add the paragraph**

Insert after the last `reingest` bullet in "Ingested documents" (the one ending "`remem embed` still exits 1 there, on purpose."):

```markdown
Every ingest leaves a row in `ingest_runs` (migration 017), written by the
service for both the spawned refresh (`trigger='auto'`) and `remem ingest`
(`trigger='manual'`): counts, per-path failures, twins, the embed error.
The refresh starts its row **before reading any file** and `reingest run`
opens its session with `autocommit=True` so that a process which dies
mid-run leaves a started, unfinished row - "crashed", not "never ran". A
Python exception is recorded as a failure with path `*` and re-raised.
`remem reingest status` renders the latest row in four distinct spellings
(never, clean, with failures, did not finish) and checks designated paths on
disk **only for the project the current directory resolves to** - the
designation stores no working directory, so any other project reads "paths
not checked" rather than letting silence pass for "all present".
`remem record status` carries one advisory line per unhealthy project.

Inside a repository, `remem ingest` identifies a chunk by its path relative
to the working tree's top level (`project.toplevel`, not `repo_root`, which
would resolve a worktree to the main checkout), so a subdirectory run or an
absolute path produces the same `src:` tag the refresh does. A path outside
the repository is refused. When a file comes in entirely new and a live
anchor with the same filename exists under another `src:` path,
`Report.twins` names it: printed to stderr with exit 0 by the CLI, recorded
in the run row by the refresh. Nothing supersedes a twin automatically - a
moved file and a document ingested twice look identical from here.
```

- [ ] **Step 2: Run the full suite**

Run: `docker compose ps && uv run pytest -q 2>&1 | tail -5`
Expected: all passed, **0 skipped**. If any test skips, Postgres is down or a marker is wrong; fix before going on.

- [ ] **Step 3: Apply the migration and try it live**

```bash
remem db status
remem db up
remem reingest status
remem reingest run; remem reingest status
remem record status
```

Expected: `db status` names `017_ingest_runs` as pending before `db up` and nothing after. `reingest status` after `run` shows `last run: auto` with counts and `all designated paths present  (checked against ...)`. `record status` shows no `!` ingest line for a healthy project.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: ingest runs, status, identity and twins"
```

- [ ] **Step 5: Whole-branch review**

Per the stored rule, dispatch the final whole-branch review on the most capable model with the full diff as a file (`git diff main...HEAD > /tmp/branch.diff` or the scratchpad), telling it its value is what a task-scoped reviewer could not see, and hand it any deferred minors for triage.
