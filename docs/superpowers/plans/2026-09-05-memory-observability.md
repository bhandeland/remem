# Memory sync observability - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Leave a durable record of every `remem memory sync`, surface it in
`remem memory status`, and raise one advisory line per unhealthy designated
project in `remem record status`.

**Architecture:** A `memory_runs` table mirroring `ingest_runs` (migration
017). `services/memory.sync()` starts the row before reading the directory
and finishes it in a `finally`, so today's CLI, `--all`, and the
hook-spawned sync in the next backlog item all record identically. Sync
moves to an autocommit session so the started row survives a crash.

**Tech Stack:** Python 3.14, psycopg 3, Typer, pytest.

**Spec:** `docs/superpowers/specs/2026-09-05-memory-observability-design.md`
- read it first. Every decision below is argued there.

## Global Constraints

- Python 3.14, `from __future__ import annotations` at the top of every
  module.
- **Layering is strict.** `cli.py` formats and decides nothing. All SQL in
  `backends/postgres/store.py`. All policy in `services/memory.py`.
  `domain.py` stays pure.
- **In prose, docstrings and comments: spaced hyphens ` - `, never em
  dashes.**
- Comments explain *why*, at length, where a decision looks arbitrary.
- **Never edit an applied migration.** Add `018_memory_runs.sql`.
  `migrate()` runs inside the caller's transaction; the caller owns
  commit/rollback.
- Timestamps use `clock_timestamp()`, never `now()` - tests run inside one
  rolled-back transaction, where `now()` gives every row an identical
  `created_at`.
- Tests needing Postgres are marked `pytest.mark.db`. **A green run means
  nothing unless the skip count is zero.**
- When watching a guard fail, clear `__pycache__` between variants
  (`find src -name __pycache__ -type d -exec rm -rf {} +`) or set
  `PYTHONDONTWRITEBYTECODE=1`. A mechanical `if X:` -> `if False and X:`
  patch adds the same byte count for every guard, and Python validates a
  `.pyc` on mtime-seconds plus size, so two variants in the same second can
  reuse each other's stale bytecode and report a false pass.
- Full suite green: `uv run pytest`. Baseline before this plan: 1169 passed,
  0 skipped.

---

### Task 1: Migration and domain types

**Files:**
- Create: `src/remem/backends/postgres/migrations/018_memory_runs.sql`
- Modify: `src/remem/domain.py` (after `IngestRun`, around line 223)
- Test: `tests/test_memory_runs_schema.py` (create, `db` marked)

**Interfaces:**
- Produces: `MemoryTrigger` (`AUTO`/`MANUAL`), and
  `MemoryRun(id, owner_id, project, trigger, started_at, finished_at,
  adopted, healed, edited, regenerated, deleted, unchanged, renamed,
  conflicts, sidecars, failures)`. Tasks 2-5 use these names.

- [ ] **Step 1: Write the failing test**

```python
"""018 creates memory_runs with the shape the service writes."""

from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate

pytestmark = pytest.mark.db


def _columns(conn, table):
    rows = conn.execute(
        "select column_name, is_nullable, data_type "
        "from information_schema.columns where table_name = %s",
        (table,),
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def test_memory_runs_exists_with_its_counts(conn):
    migrate(conn)
    cols = _columns(conn, "memory_runs")
    for name in ("adopted", "healed", "edited", "regenerated", "deleted",
                 "unchanged"):
        assert cols[name][0] == "NO", name
    for name in ("renamed", "conflicts", "sidecars", "failures"):
        assert cols[name][1] == "jsonb", name
    # finished_at nullable is the whole crash-detection design.
    assert cols["finished_at"][0] == "YES"
    assert cols["started_at"][0] == "NO"


def test_the_trigger_check_rejects_an_unknown_value(conn):
    import psycopg
    migrate(conn)
    conn.execute(
        "insert into principals (id, handle) values "
        "('00000000-0000-0000-0000-000000000001', 'trigger-check')"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "insert into memory_runs (id, owner_id, project, trigger) values "
            "('00000000-0000-0000-0000-000000000002',"
            " '00000000-0000-0000-0000-000000000001', 'p', 'sideways')"
        )
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_memory_runs_schema.py -v`
Expected: FAIL - `memory_runs` has no columns (KeyError), not a skip. A skip
means Postgres is down: `docker compose up -d`.

- [ ] **Step 3: Write the migration**

Create `src/remem/backends/postgres/migrations/018_memory_runs.sql`:

```sql
-- One row per `remem memory sync`, per project - the record a sync
-- otherwise leaves only in a terminal scrollback. Built before the
-- hook-spawned sync rather than after, because that caller is the one with
-- no terminal, and because a conflict sidecar outlives the run that wrote
-- it: nothing ever deletes a `.remem-conflict.md`, so an unresolved
-- conflict is a permanent condition visible today only from that project's
-- own directory.
--
-- A started row with `finished_at` null is a statement, not a gap: the
-- process died between starting and finishing. That only works because
-- `remem memory sync` commits the started row before reading any file,
-- which is why it moved to an autocommit session.
--
-- Rows are kept indefinitely, as events and ingest_runs rows are. Nothing
-- prunes them.
create table memory_runs (
  id uuid primary key,
  owner_id uuid not null references principals(id),
  project text not null,
  -- 'manual' for `remem memory sync`, 'auto' for the hook-spawned sync that
  -- does not exist yet. Recorded now because that caller is the reason this
  -- table exists, and a column added later costs a migration for a case
  -- already known to be coming. Text with a check rather than an enum: two
  -- values, and the check reads the same.
  trigger text not null check (trigger in ('manual', 'auto')),
  started_at timestamptz not null default clock_timestamp(),
  finished_at timestamptz,
  adopted int not null default 0,
  healed int not null default 0,
  edited int not null default 0,
  regenerated int not null default 0,
  deleted int not null default 0,
  unchanged int not null default 0,
  -- [["old","new"], ...]. Named rather than only counted: a rename re-tags
  -- an entry, which is a write the user did not ask for by name.
  renamed jsonb not null default '[]'::jsonb,
  -- Names changed on both sides.
  conflicts jsonb not null default '[]'::jsonb,
  -- The subset of `conflicts` that actually wrote a `.remem-conflict.md`.
  -- A dry run writes none, and neither does a file edited for an entry that
  -- left the collection, so the two lists are not interchangeable.
  sidecars jsonb not null default '[]'::jsonb,
  -- [{"name": ..., "reason": ...}]. A Python exception is recorded at the
  -- name '*'.
  failures jsonb not null default '[]'::jsonb
);

-- Every read is "the latest row for this project".
create index memory_runs_latest_idx
  on memory_runs (owner_id, project, started_at desc);
```

- [ ] **Step 4: Add the domain types**

In `src/remem/domain.py`, after `IngestRun`:

```python
class MemoryTrigger(StrEnum):
    """Who started a memory sync: a person, or the spawned hook."""

    AUTO = "auto"
    MANUAL = "manual"


@dataclass(slots=True)
class MemoryRun:
    """One `remem memory sync` invocation's record - see 018_memory_runs.sql.

    `finished_at` is None for a row whose process died before finishing.
    The four lists are plain dicts and tuples, the shape they are stored in,
    because the only readers are a status renderer and `--json`.
    """

    id: UUID
    owner_id: UUID
    project: str
    trigger: MemoryTrigger
    started_at: datetime | None = None
    finished_at: datetime | None = None
    adopted: int = 0
    healed: int = 0
    edited: int = 0
    regenerated: int = 0
    deleted: int = 0
    unchanged: int = 0
    renamed: list[list[str]] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    sidecars: list[str] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
```

- [ ] **Step 5: Run the tests and watch them pass**

Run: `uv run pytest tests/test_memory_runs_schema.py -v`
Expected: 2 passed, 0 skipped.

- [ ] **Step 6: Commit**

```bash
git add src/remem/backends/postgres/migrations/018_memory_runs.sql src/remem/domain.py tests/test_memory_runs_schema.py
git commit -m "Add the memory_runs table"
```

---

### Task 2: Store methods

**Files:**
- Modify: `src/remem/store.py` (Protocol, beside the ingest-run methods
  around line 123-143)
- Modify: `src/remem/backends/postgres/store.py` (after
  `latest_ingest_run`, around line 1022)
- Test: `tests/test_memory_runs_store.py` (create, `db` marked)

**Interfaces:**
- Consumes: `MemoryRun`, `MemoryTrigger` (Task 1).
- Produces:
  - `start_memory_run(owner_id, project, trigger) -> MemoryRun`
  - `finish_memory_run(run_id, owner_id, *, adopted, healed, edited,
    regenerated, deleted, unchanged, renamed, conflicts, sidecars,
    failures) -> None`
  - `latest_memory_run(owner_id, project) -> MemoryRun | None`
  Task 3 calls all three.

Read `start_ingest_run` / `finish_ingest_run` / `latest_ingest_run`
(`backends/postgres/store.py:969-1022`) first and mirror them, including
`ingest_run_columns()` and `_row_to_ingest_run` - write the `memory_run`
equivalents beside them.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_memory_runs_store.py`:

```python
"""The memory_runs reads and writes."""

from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import MemoryTrigger
from remem.store import NotOwner

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


def test_a_started_run_has_no_finish(store):
    owner = store.ensure_principal("runs-start")
    run = store.start_memory_run(owner.id, "p", MemoryTrigger.MANUAL)
    assert run.started_at is not None
    assert run.finished_at is None
    assert run.trigger is MemoryTrigger.MANUAL


def test_finishing_records_every_count_and_list(store):
    owner = store.ensure_principal("runs-finish")
    run = store.start_memory_run(owner.id, "p", MemoryTrigger.MANUAL)

    store.finish_memory_run(
        run.id, owner.id, adopted=1, healed=2, edited=3, regenerated=4,
        deleted=5, unchanged=6, renamed=[["old", "new"]],
        conflicts=["c"], sidecars=["c"],
        failures=[{"name": "bad", "reason": "boom"}],
    )

    latest = store.latest_memory_run(owner.id, "p")
    assert latest.finished_at is not None
    assert (latest.adopted, latest.healed, latest.edited) == (1, 2, 3)
    assert (latest.regenerated, latest.deleted, latest.unchanged) == (4, 5, 6)
    assert latest.renamed == [["old", "new"]]
    assert latest.conflicts == ["c"] and latest.sidecars == ["c"]
    assert latest.failures == [{"name": "bad", "reason": "boom"}]


def test_latest_is_the_newest_row(store):
    owner = store.ensure_principal("runs-latest")
    store.start_memory_run(owner.id, "p", MemoryTrigger.MANUAL)
    second = store.start_memory_run(owner.id, "p", MemoryTrigger.AUTO)
    assert store.latest_memory_run(owner.id, "p").id == second.id


def test_latest_is_none_when_a_project_never_ran(store):
    owner = store.ensure_principal("runs-none")
    assert store.latest_memory_run(owner.id, "never") is None


def test_runs_are_per_project(store):
    owner = store.ensure_principal("runs-project")
    a = store.start_memory_run(owner.id, "one", MemoryTrigger.MANUAL)
    b = store.start_memory_run(owner.id, "two", MemoryTrigger.MANUAL)
    assert store.latest_memory_run(owner.id, "one").id == a.id
    assert store.latest_memory_run(owner.id, "two").id == b.id


def test_another_principals_run_is_never_returned(store):
    """A fresh fixture guarantees no other rows, which is why one is seeded."""
    mine = store.ensure_principal("runs-mine")
    theirs = store.ensure_principal("runs-theirs")
    store.start_memory_run(theirs.id, "p", MemoryTrigger.MANUAL)

    assert store.latest_memory_run(mine.id, "p") is None


def test_finishing_another_principals_run_is_refused(store):
    mine = store.ensure_principal("finish-mine")
    theirs = store.ensure_principal("finish-theirs")
    run = store.start_memory_run(theirs.id, "p", MemoryTrigger.MANUAL)

    with pytest.raises(NotOwner):
        store.finish_memory_run(
            run.id, mine.id, adopted=0, healed=0, edited=0, regenerated=0,
            deleted=0, unchanged=0, renamed=[], conflicts=[], sidecars=[],
            failures=[],
        )

    assert store.latest_memory_run(theirs.id, "p").finished_at is None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_memory_runs_store.py -v`
Expected: 7 FAIL with `AttributeError: ... 'start_memory_run'`.

- [ ] **Step 3: Add the Protocol methods**

In `src/remem/store.py`, beside the ingest-run methods, and add `MemoryRun`
/ `MemoryTrigger` to the domain import block:

```python
    def start_memory_run(
        self, owner_id: UUID, project: str, trigger: MemoryTrigger
    ) -> MemoryRun: ...

    def finish_memory_run(
        self, run_id: UUID, owner_id: UUID, *,
        adopted: int, healed: int, edited: int, regenerated: int,
        deleted: int, unchanged: int, renamed: list[list[str]],
        conflicts: list[str], sidecars: list[str], failures: list[dict],
    ) -> None: ...

    def latest_memory_run(
        self, owner_id: UUID, project: str
    ) -> MemoryRun | None: ...
```

- [ ] **Step 4: Implement them**

In `src/remem/backends/postgres/store.py`, after `latest_ingest_run`. Add
`MemoryRun` / `MemoryTrigger` to the domain import block, and these two
module-level helpers beside `ingest_run_columns` / `_row_to_ingest_run`:

```python
MEMORY_RUN_FIELDS = [
    "id", "owner_id", "project", "trigger", "started_at", "finished_at",
    "adopted", "healed", "edited", "regenerated", "deleted", "unchanged",
    "renamed", "conflicts", "sidecars", "failures",
]


def memory_run_columns() -> str:
    return ", ".join(MEMORY_RUN_FIELDS)


def _row_to_memory_run(row: dict) -> MemoryRun:
    return MemoryRun(
        id=row["id"],
        owner_id=row["owner_id"],
        project=row["project"],
        trigger=MemoryTrigger(row["trigger"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        adopted=row["adopted"],
        healed=row["healed"],
        edited=row["edited"],
        regenerated=row["regenerated"],
        deleted=row["deleted"],
        unchanged=row["unchanged"],
        renamed=list(row["renamed"] or []),
        conflicts=list(row["conflicts"] or []),
        sidecars=list(row["sidecars"] or []),
        failures=list(row["failures"] or []),
    )
```

And the methods:

```python
    def start_memory_run(
        self, owner_id: UUID, project: str, trigger: MemoryTrigger
    ) -> MemoryRun:
        """The row that exists before any file is read.

        Committed by the caller's autocommit session, which is what makes a
        `finished_at` of null mean "the process died" rather than "the
        transaction rolled back".
        """
        run_id = new_id()
        with self._cur() as cur:
            cur.execute(
                f"""
                insert into memory_runs (id, owner_id, project, trigger)
                values (%s, %s, %s, %s)
                returning {memory_run_columns()}
                """,
                (run_id, owner_id, project, str(trigger)),
            )
            return _row_to_memory_run(cur.fetchone())

    def finish_memory_run(
        self, run_id: UUID, owner_id: UUID, *,
        adopted: int, healed: int, edited: int, regenerated: int,
        deleted: int, unchanged: int, renamed: list[list[str]],
        conflicts: list[str], sidecars: list[str], failures: list[dict],
    ) -> None:
        with self._cur() as cur:
            cur.execute(
                """
                update memory_runs
                   set finished_at = clock_timestamp(),
                       adopted = %s, healed = %s, edited = %s,
                       regenerated = %s, deleted = %s, unchanged = %s,
                       renamed = %s::jsonb, conflicts = %s::jsonb,
                       sidecars = %s::jsonb, failures = %s::jsonb
                 where id = %s and owner_id = %s
                """,
                (adopted, healed, edited, regenerated, deleted, unchanged,
                 json.dumps(renamed), json.dumps(conflicts),
                 json.dumps(sidecars), json.dumps(failures),
                 run_id, owner_id),
            )
            if cur.rowcount == 0:
                raise NotOwner(f"memory run {run_id} is not owned by {owner_id}")

    def latest_memory_run(
        self, owner_id: UUID, project: str
    ) -> MemoryRun | None:
        with self._cur() as cur:
            cur.execute(
                f"""
                select {memory_run_columns()} from memory_runs
                 where owner_id = %s and project = %s
                 order by started_at desc
                 limit 1
                """,
                (owner_id, project),
            )
            row = cur.fetchone()
        return _row_to_memory_run(row) if row else None
```

- [ ] **Step 5: Run the tests and watch them pass**

Run: `uv run pytest tests/test_memory_runs_store.py -v`
Expected: 7 passed, 0 skipped.

- [ ] **Step 6: Commit**

```bash
git add src/remem/store.py src/remem/backends/postgres/store.py tests/test_memory_runs_store.py
git commit -m "Read and write memory run rows"
```

---

### Task 3: `sync()` records its own run, and the CLI goes autocommit

**Files:**
- Modify: `src/remem/services/memory.py` (`sync`, from line 553)
- Modify: `src/remem/cli.py` (`memory_sync`, the `with _session()` call)
- Test: `tests/test_memory_run_recording.py` (create, `db` marked)

**Interfaces:**
- Consumes: the three store methods (Task 2).
- Produces: `sync(..., trigger: MemoryTrigger = MemoryTrigger.MANUAL)` -
  same return type (`Report`), one new keyword argument. Task 4 reads the
  rows it writes.

**The shape:** start the row immediately after the `NotDesignated` check
and before `load_watermarks`, wrap the body in `try/finally`, and finish the
row in the `finally`. `NotDesignated` is raised **before** the row is
started, on purpose: nothing ran, so nothing should be recorded.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_memory_run_recording.py`:

```python
"""What sync() records, including from a second connection mid-run."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import CollectionQuery, MemoryTrigger
from remem.services import kb
from remem.services import memory as memory_service

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


def _designate(store, owner_id, project, directory):
    """Designate `project`, recording `directory` as its working dir.

    The collection has to exist first - `memory.designate` calls `kb.get`
    and raises CollectionNotFound otherwise - and it needs an explicit
    CollectionQuery, because an empty query matches nothing forever. This
    mirrors `_designated` in tests/test_memory_sync.py.
    """
    kb.create(store, owner_id, slug="mem", title="Memory", project=project,
              query=CollectionQuery(project=project))
    memory_service.designate(store, owner_id, project, "mem",
                             working_dir=str(directory))


def test_a_dry_run_records_nothing(store, tmp_path):
    """A dry run changes nothing, so 'last run' must not describe it."""
    owner = store.ensure_principal("run-dry")
    _designate(store, owner.id, "p", tmp_path)

    memory_service.sync(store, owner.id, project="p", directory=tmp_path,
                        dry_run=True)

    assert store.latest_memory_run(owner.id, "p") is None


def test_a_real_run_is_recorded_and_finished(store, tmp_path):
    owner = store.ensure_principal("run-real")
    _designate(store, owner.id, "p", tmp_path)

    memory_service.sync(store, owner.id, project="p", directory=tmp_path)

    run = store.latest_memory_run(owner.id, "p")
    assert run is not None
    assert run.finished_at is not None
    assert run.trigger is MemoryTrigger.MANUAL


def test_an_exception_is_recorded_and_re_raised(store, tmp_path, monkeypatch):
    owner = store.ensure_principal("run-boom")
    _designate(store, owner.id, "p", tmp_path)
    monkeypatch.setattr(
        memory_service, "load_watermarks",
        lambda directory: (_ for _ in ()).throw(OSError("disk gone")),
    )

    with pytest.raises(OSError):
        memory_service.sync(store, owner.id, project="p", directory=tmp_path)

    run = store.latest_memory_run(owner.id, "p")
    assert run.finished_at is not None
    assert run.failures == [{"name": "*", "reason": "disk gone"}]


def test_an_undesignated_project_records_no_run(store, tmp_path):
    owner = store.ensure_principal("run-undesignated")
    with pytest.raises(memory_service.NotDesignated):
        memory_service.sync(store, owner.id, project="p", directory=tmp_path)
    assert store.latest_memory_run(owner.id, "p") is None
```

Add, in a **separate** file `tests/test_memory_run_visibility.py`, the
test that actually proves the autocommit change. It uses `live_dsn`, because
the started row must be read from a *second connection* - a same-connection
read would pass under the old single-transaction code and prove nothing:

```python
"""The started row is visible to another connection before sync finishes."""

from __future__ import annotations

import psycopg
import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import MemoryTrigger

pytestmark = pytest.mark.db


def test_a_started_row_is_visible_from_another_connection(live_dsn, tmp_path):
    with psycopg.connect(live_dsn) as setup:
        migrate(setup)
        setup.commit()

    with psycopg.connect(live_dsn, autocommit=True) as writer:
        store = PostgresStore(writer)
        owner = store.ensure_principal("visible")
        run = store.start_memory_run(owner.id, "p", MemoryTrigger.MANUAL)

        # A second connection, while the "run" is still in progress.
        with psycopg.connect(live_dsn) as reader:
            seen = PostgresStore(reader).latest_memory_run(owner.id, "p")

        assert seen is not None and seen.id == run.id
        assert seen.finished_at is None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_memory_run_recording.py tests/test_memory_run_visibility.py -v`
Expected: the recording tests FAIL (`latest_memory_run` returns None because
nothing records yet); the visibility test PASSES already, since it drives the
store directly - keep it, it is the regression guard for the session change
in Step 4.

- [ ] **Step 3: Record the run inside `sync()`**

In `src/remem/services/memory.py`, change the signature and wrap the body:

```python
def sync(
    store: Store,
    owner_id: UUID,
    *,
    project: str,
    directory: Path,
    dry_run: bool = False,
    trigger: MemoryTrigger = MemoryTrigger.MANUAL,
) -> Report:
```

Immediately after the `NotDesignated` check - so an undesignated project
records nothing, because nothing ran - and before `load_watermarks`:

```python
    # The row is started before any file is read, so a process killed
    # partway leaves a started-and-unfinished row: "crashed", not "never
    # ran". That only holds because the caller's session is autocommit.
    #
    # A dry run records nothing at all. It changes neither the disk nor the
    # store, so a row for it would make "last run" describe a state that
    # never existed.
    run = (
        None if dry_run
        else store.start_memory_run(owner_id, project, trigger)
    )
```

Wrap everything from `report = Report()` to the `return report` in
`try:` / `finally:`, and in the `finally`:

```python
    finally:
        if run is not None:
            failures = [{"name": n, "reason": r} for n, r in report.failures]
            if error is not None:
                # Recorded as a failure rather than swallowed, and then
                # re-raised: sync stays fail-loud, and the row still says
                # what happened. Path '*' because the exception is about the
                # run, not about one file.
                failures.append({"name": "*", "reason": str(error)})
            store.finish_memory_run(
                run.id, owner_id,
                adopted=report.adopted, healed=report.healed,
                edited=report.edited, regenerated=report.regenerated,
                deleted=report.deleted, unchanged=report.unchanged,
                renamed=[[old, new] for old, new in report.renamed],
                conflicts=list(report.conflicts),
                sidecars=list(report.sidecars),
                failures=failures,
            )
```

Capture the exception with `except BaseException as exc: error = exc; raise`
before the `finally`, initialising `error = None` alongside `run`. `report`
must be constructed before the `try` so the `finally` can always read it.

- [ ] **Step 4: Move the CLI to an autocommit session**

In `src/remem/cli.py`, in `memory_sync`, change `with _session() as s:` to:

```python
    # autocommit, like `remem reingest run` and `remem events process`: the
    # started run row has to be committed before any file is read, or a
    # crash rolls it back and "crashed" becomes indistinguishable from
    # "never ran". It also stops sync's two halves disagreeing - files are
    # written to disk as the sync goes, so a single transaction only ever
    # rolled the store back and left the disk moved.
    with _session(autocommit=True) as s:
```

Do the same in `_memory_sync_all`.

- [ ] **Step 5: Run the tests and watch them pass**

Run: `uv run pytest tests/test_memory_run_recording.py tests/test_memory_run_visibility.py tests/test_memory_sync.py -v`
Expected: all pass, 0 skipped. The existing memory sync tests must stay
green - if any went red, the `try/finally` changed sync's behaviour and that
is a bug in this task, not a test to update.

- [ ] **Step 6: Commit**

```bash
git add src/remem/services/memory.py src/remem/cli.py tests/test_memory_run_recording.py tests/test_memory_run_visibility.py
git commit -m "Record every memory sync as a run row"
```

---

### Task 4: The history line in `remem memory status`

**Files:**
- Modify: `src/remem/services/memory.py` (`Status`, `status`)
- Modify: `src/remem/cli.py` (`memory_status`)
- Test: `tests/test_memory_status_history.py` (create, NO `db` marker)

**Interfaces:**
- Consumes: `latest_memory_run` (Task 2), `MemoryRun` (Task 1).
- Produces: `Status.run: MemoryRun | None` and
  `render_run(run: MemoryRun | None) -> str` in `services/memory.py`.
  Task 5 reuses `render_run`'s judgement, not its string.

- [ ] **Step 1: Write the failing test**

Create `tests/test_memory_status_history.py`:

```python
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
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_memory_status_history.py -v`
Expected: FAIL, `ImportError: cannot import name 'render_run'`.

- [ ] **Step 3: Implement**

In `services/memory.py`, add to `Status` (after `conflicts`):

```python
    #: The latest run for this project, or None if it has never synced.
    #: Distinct from the fields above on purpose: those describe the
    #: directory NOW, this describes what last happened to it, and a reader
    #: must not have to infer one from the other.
    run: MemoryRun | None = None
```

In `status()`, after the designation check, set
`out.run = store.latest_memory_run(owner_id, project)`.

Add:

```python
def render_run(run: MemoryRun | None) -> str:
    """The one-line history, in four distinct spellings.

    Four rather than three because "finished" and "finished with something
    wrong" are different facts, and a reader scanning for trouble should not
    have to parse counts to find it. Follows `remem reingest status`.
    """
    if run is None:
        return "  never synced"
    when = run.started_at.strftime("%Y-%m-%d %H:%M") if run.started_at else "?"
    if run.finished_at is None:
        return (f"  last sync {when} did not finish - the process was "
                f"killed partway")
    counts = (f"{run.adopted} adopted, {run.edited} edited, "
              f"{run.regenerated} regenerated, {run.deleted} deleted, "
              f"{len(run.renamed)} renamed, {run.unchanged} unchanged")
    trouble = []
    if run.conflicts:
        trouble.append(f"{len(run.conflicts)} conflict(s)")
    if run.failures:
        trouble.append(f"{len(run.failures)} failure(s)")
    if trouble:
        return f"  last sync {when}: {counts} - {', '.join(trouble)}"
    return f"  last sync {when}: {counts}"
```

In `cli.py`'s `memory_status`, after the entries/files/stale line:

```python
    typer.echo(memory_service.render_run(st.run))
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run pytest tests/test_memory_status_history.py -v`
Expected: 6 passed, 0 skipped (no `db` marker - these run on CI).

- [ ] **Step 5: Commit**

```bash
git add src/remem/services/memory.py src/remem/cli.py tests/test_memory_status_history.py
git commit -m "Show the latest sync in remem memory status"
```

---

### Task 5: The advisory in `remem record status`

**Files:**
- Modify: `src/remem/services/memory.py` (add `advisories`)
- Modify: `src/remem/services/events.py` (`Report`, `status`, `render`,
  the `--json` dict - lines 149, 161, 199, 254, 295)
- Modify: `src/remem/cli.py` (`record_status`, beside the ingest call at
  line 1369)
- Test: `tests/test_memory_advisories.py` (create, `db` marked)

**Interfaces:**
- Consumes: `designations()`, `status()`, `Status.run` (Task 4).
- Produces: `memory.advisories(store, owner_id) -> list[str]` and
  `events.Report.memory_advisories`.

**Read first:** `services/ingest.advisories` (line 803) - this mirrors it,
including that only **designated** projects raise a line and that every
pointer names its scope. Note the difference the spec calls out:
`memory.status()` answers for one project, so the sweep over
`designations()` lives here rather than below.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_memory_advisories.py`:

```python
"""One line per unhealthy designated project."""

from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import CollectionQuery, MemoryTrigger
from remem.services import kb
from remem.services import memory as memory_service

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


def _designate(store, owner_id, project, directory):
    """Designate `project`, recording `directory` as its working dir.

    The collection has to exist first - `memory.designate` calls `kb.get`
    and raises CollectionNotFound otherwise - and it needs an explicit
    CollectionQuery, because an empty query matches nothing forever. This
    mirrors `_designated` in tests/test_memory_sync.py.
    """
    kb.create(store, owner_id, slug="mem", title="Memory", project=project,
              query=CollectionQuery(project=project))
    memory_service.designate(store, owner_id, project, "mem",
                             working_dir=str(directory))


def test_a_healthy_project_raises_nothing(store, tmp_path):
    owner = store.ensure_principal("adv-healthy")
    _designate(store, owner.id, "p", tmp_path)
    run = store.start_memory_run(owner.id, "p", MemoryTrigger.MANUAL)
    store.finish_memory_run(
        run.id, owner.id, adopted=0, healed=0, edited=0, regenerated=0,
        deleted=0, unchanged=0, renamed=[], conflicts=[], sidecars=[],
        failures=[])

    assert memory_service.advisories(store, owner.id) == []


def test_a_never_synced_project_is_named(store, tmp_path):
    owner = store.ensure_principal("adv-never")
    _designate(store, owner.id, "p", tmp_path)

    lines = memory_service.advisories(store, owner.id)

    assert len(lines) == 1
    assert "never synced" in lines[0]
    assert "p" in lines[0]


def test_an_unfinished_run_is_named(store, tmp_path):
    owner = store.ensure_principal("adv-unfinished")
    _designate(store, owner.id, "p", tmp_path)
    store.start_memory_run(owner.id, "p", MemoryTrigger.MANUAL)

    lines = memory_service.advisories(store, owner.id)

    assert "did not finish" in lines[0]


def test_failures_are_named(store, tmp_path):
    owner = store.ensure_principal("adv-failures")
    _designate(store, owner.id, "p", tmp_path)
    run = store.start_memory_run(owner.id, "p", MemoryTrigger.MANUAL)
    store.finish_memory_run(
        run.id, owner.id, adopted=0, healed=0, edited=0, regenerated=0,
        deleted=0, unchanged=0, renamed=[], conflicts=[], sidecars=[],
        failures=[{"name": "bad", "reason": "boom"}])

    assert "1 failure" in memory_service.advisories(store, owner.id)[0]


def test_a_sidecar_on_disk_is_named(store, tmp_path):
    """The condition that outlives every run - nothing deletes a sidecar."""
    owner = store.ensure_principal("adv-sidecar")
    _designate(store, owner.id, "p", tmp_path)
    run = store.start_memory_run(owner.id, "p", MemoryTrigger.MANUAL)
    store.finish_memory_run(
        run.id, owner.id, adopted=0, healed=0, edited=0, regenerated=0,
        deleted=0, unchanged=0, renamed=[], conflicts=[], sidecars=[],
        failures=[])
    (tmp_path / "note.remem-conflict.md").write_text("x")

    assert "conflict" in memory_service.advisories(store, owner.id)[0]


def test_a_designation_with_no_directory_is_skipped_by_name(store):
    """Written before migration 015. Named, never guessed at."""
    owner = store.ensure_principal("adv-nodir")
    # Straight to the store: `memory.designate` would need a collection,
    # and this test is about a row that predates the working_dir column.
    store.set_memory_collection(owner.id, "p", "mem", None)

    lines = memory_service.advisories(store, owner.id)

    assert len(lines) == 1
    assert "re-designate" in lines[0].lower()


def test_a_missing_directory_is_not_reported_as_clean(store, tmp_path):
    owner = store.ensure_principal("adv-gone")
    gone = tmp_path / "gone"
    _designate(store, owner.id, "p", gone)
    run = store.start_memory_run(owner.id, "p", MemoryTrigger.MANUAL)
    store.finish_memory_run(
        run.id, owner.id, adopted=0, healed=0, edited=0, regenerated=0,
        deleted=0, unchanged=0, renamed=[], conflicts=[], sidecars=[],
        failures=[])

    assert "does not exist" in memory_service.advisories(store, owner.id)[0]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_memory_advisories.py -v`
Expected: 7 FAIL with `AttributeError: module ... has no attribute
'advisories'`.

- [ ] **Step 3: Implement `advisories`**

In `services/memory.py`:

```python
#: Where a person goes after reading an advisory line. Carries its scope,
#: because a pointer leading to a screen that contradicts the line teaches
#: the user the line lies.
STATUS_POINTER = "run `remem memory status --project {project}`"


def advisories(store: Store, owner_id: UUID) -> list[str]:
    """One line per designated project whose memory sync needs attention.

    For `remem record status`, the fail-loud half of a fail-soft pipeline,
    which already carries the doctor and ingest advisories the same way.

    Unlike `ingest.advisories`, this checks every designated project's
    directory rather than only the current one: `memory_settings` records
    the working directory a designation was made from (migration 015), so
    the answer is stored rather than guessed. A row written before 015 has
    none, and is named in the output rather than skipped silently -
    re-designating is the fix.
    """
    lines: list[str] = []
    for d in designations(store, owner_id):
        if d.working_dir is None:
            lines.append(
                f"{d.project}: designated before its working directory was "
                f"recorded, so its memory directory cannot be found - "
                f"re-designate it from that directory."
            )
            continue

        directory = Path(d.working_dir)
        pointer = STATUS_POINTER.format(project=d.project)
        run = store.latest_memory_run(owner_id, d.project)

        if run is None:
            lines.append(f"{d.project}: designated but never synced - "
                         f"{pointer}")
            continue
        if run.finished_at is None:
            lines.append(f"{d.project}: the last memory sync did not finish "
                         f"- {pointer}")
            continue

        trouble = []
        if not directory.exists():
            # Distinct from "no conflicts": an absent directory is a
            # different fact from a clean one, and reporting it as clean is
            # the confident lie a diagnostic must never tell.
            trouble.append(f"its memory directory {directory} does not exist")
        else:
            sidecars = list(directory.glob(f"*{CONFLICT_SUFFIX}"))
            if sidecars:
                trouble.append(
                    f"{len(sidecars)} unresolved conflict sidecar(s) on disk"
                )
        if run.failures:
            trouble.append(f"{len(run.failures)} failure(s) in the last sync")
        if trouble:
            lines.append(f"{d.project}: {'; '.join(trouble)} - {pointer}")
    return lines
```

- [ ] **Step 4: Plumb it through `events.status`**

In `services/events.py`, mirror `ingest_advisories` exactly at all five
sites: the `Report` field (line 149), the `status()` parameter (line 161)
and its assignment (line 199), the `render()` loop (line 259), and the
`--json` dict (line 295):

```python
    #: Same reason `ingest_advisories` is here: this module is about events,
    #: and a memory sync that never finished is not an event - but
    #: `record status` is the one screen a person checks, so it carries it.
    memory_advisories: list[str] = field(default_factory=list)
```

In `cli.py`'s `record_status`, beside the ingest call:

```python
        # Wrapped like the doctor and ingest calls above, and for the same
        # reason: a memory advisory that cannot be computed must not take
        # down the events status it decorates.
        try:
            memory_advisories = memory_service.advisories(s.store, s.owner.id)
        except Exception:
            memory_advisories = []
```

and pass `memory_advisories=memory_advisories` to `events.status`.

- [ ] **Step 5: Run the tests and watch them pass**

Run: `uv run pytest tests/test_memory_advisories.py tests/test_record_status_cli.py -v`

(If that second file does not exist, run `uv run pytest -k "record_status"`
instead.)
Expected: all pass, 0 skipped.

- [ ] **Step 6: Watch each advisory condition fail**

For each of the four conditions plus the two directory cases, neutralise the
branch that produces it (`if X:` -> `if False and X:`), run the matching
test, confirm RED, restore. **Clear `__pycache__` after every patch and
every restore** - see Global Constraints.

- [ ] **Step 7: Commit**

```bash
git add src/remem/services/memory.py src/remem/services/events.py src/remem/cli.py tests/test_memory_advisories.py
git commit -m "Advise on unhealthy memory syncs in remem record status"
```

---

### Task 6: Documentation and the whole suite

**Files:**
- Modify: `CLAUDE.md` (the "Claude Code memory" section)

- [ ] **Step 1: Extend the CLAUDE.md section**

Add to the end of the "Claude Code memory" bullet list:

```markdown
- Every sync leaves a row in `memory_runs` (migration 018), written by the
  service so that `remem memory sync`, `--all`, and any future hook-spawned
  run record identically - the last of those being the caller with no
  terminal, and the reason the table was built before it exists. One row per
  **project**: `--all` writes six, and a single row could not say which one
  failed. A `--dry-run` writes none, because a row for it would make "last
  run" describe a state that never existed.
- `remem memory sync` therefore opens with `autocommit=True`, like `remem
  reingest run`: the started row must be committed before any file is read,
  or a crash rolls it back and "crashed" is indistinguishable from "never
  ran". This also stops sync's two halves disagreeing - files are written to
  disk as it goes, so a single transaction only ever rolled the store back
  and left the disk moved. Sync is idempotent and re-runnable, and both
  gates hold on the next run exactly as they held on this one.
- `remem memory status` renders the latest run in four distinct spellings
  (never, clean, with conflicts or failures, did not finish). Those describe
  what last *happened*; `stale`, `overlap` and `conflicts` describe the
  directory *now*, and a reader must not have to infer one from the other.
- `memory.advisories()` raises one line per unhealthy designated project in
  `remem record status`: an unresolved conflict sidecar, a run that did not
  finish, failures in the last run, or a designation that has never synced.
  Unlike `reingest status`, it checks **every** designated project's
  directory, because `memory_settings` records the working directory
  (migration 015) - the answer is stored, not guessed. A pre-015 row with no
  directory is named and told to re-designate, never guessed at, and a
  recorded directory that no longer exists is reported as missing rather
  than as clean.
```

- [ ] **Step 2: Run the whole suite**

Run: `uv run pytest`
Expected: all pass, **0 skipped**. Baseline was 1169; this plan adds roughly
30 tests.

- [ ] **Step 3: Exercise it by hand**

```bash
remem db up
remem memory status
remem memory sync --dry-run && remem memory status   # still says never/last, unchanged
remem memory sync && remem memory status
remem record status | tail -20
```

Confirm the dry run changed no history line, that the real sync did, and
that a healthy project raises no advisory.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "Document memory sync observability"
```
