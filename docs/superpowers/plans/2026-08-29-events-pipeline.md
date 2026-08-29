# Events Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace transcript-based capture with a harness-neutral pipeline that records raw events into Postgres, extracts entries from them on an idleness trigger, and can re-run that extraction because the raw material never left the database.

**Architecture:** A hook writes one row per thing that happened (`events`), in full, unparsed. A later `remem events process` finds sessions that have gone quiet, claims an `extract_jobs` row, feeds the session's events to the pinned model, and writes entries with `origin='extracted'` plus `entry_events` provenance rows. Prune is a command the user types, never a schedule. The vocabulary the spec settled - note, event, record, extract, process, prune, harness - is applied to the schema, the code and the CLI in one pass at the front of the plan, so no task after Task 1 is written in two languages.

**Tech Stack:** Python 3.14, Postgres 18 (`pgvector/pgvector:pg18`) with psycopg 3 and hand-written SQL, Typer, FastMCP, pytest. The extractor shells out to `claude -p`, as capture did.

**Spec:** `docs/superpowers/specs/2026-08-28-events-and-recall-design.md`

This plan implements the **second** of the spec's three subsystems. Semantic recall shipped already (`docs/superpowers/plans/2026-08-28-semantic-recall.md`, migration `006`). The **Cursor and opencode adapters are out of scope** - the spec says only claude-code ships in this change, and the other two get their own plan. Nothing here writes `.cursor/rules/*.mdc` or a JS plugin.

## Global Constraints

- **Python 3.14.** `from __future__ import annotations` at the top of every module; `StrEnum` for enums; `uuid7` via `domain.new_id()`.
- **Frontends parse and format; they never decide.** Every policy branch - the opt-in gate, the idle window, what prune may delete, the attempt cap - lives in `services/`. A policy branch in `cli.py` is a bug in this plan's execution.
- **`store.py` is the portability seam.** Every new database operation goes on the `Store` Protocol first, then into `backends/postgres/store.py`. No SQL outside `backends/`.
- **Ownership is enforced inside the store**, not by callers. A write that could touch another principal's row raises `NotOwner`.
- **`session.open_session()` is the only way to reach the database.** Nothing outside `session.py` and `backends/` imports psycopg.
- **Migrations are numbered `.sql` files, applied in filename order, never edited once applied.** `migrate()` runs inside the caller's transaction; the caller owns commit and rollback.
- **Timestamps use `clock_timestamp()`, never `now()`.** Tests run inside one rolled-back transaction, where `now()` gives every row an identical timestamp and makes `order by ... desc` non-deterministic.
- **Hooks are fail-soft: exit 0 unconditionally, print nothing on error, never raise.** `REMEM_HOOK_DEBUG=1` explains to **stderr** - stdout is the context block and nothing else.
- **Cron commands are the inverse: quiet on success, non-zero and explanatory on failure.**
- **Comments explain *why*, at length, especially where a decision looks arbitrary.** Match the surrounding density, which is high.
- **In prose, docs, comments and CLI output: spaced hyphens ` - `, never em dashes.**
- **A green pytest run means nothing unless the skip count is zero.** Before trusting any run: `docker compose ps` shows healthy on port 5433, and the summary reads `0 skipped`. **Baseline before this plan is 543 passed, 0 skipped.**
- **The `store` and `owner` fixtures are per-file, not in `conftest.py`.** Every database test module defines its own, exactly as `tests/test_store_entries.py:10-18` does: `store` runs `migrate(conn)` and returns `PostgresStore(conn)`, `owner` returns `store.ensure_principal("brandon")`. Repeat those four lines in each new test file, and mark the module `pytestmark = pytest.mark.db`.
- **`uv tool install --editable .`** after any `pyproject.toml` change, or the `remem` on PATH - which is what the hooks call - will not have the new wiring.

## Decisions this plan settles

The spec left four things to planning. They are ruled here so no task stops to think.

**1. Four migration files, not one `007_events.sql`.** The spec describes one migration in six steps. Six steps in one file is one all-or-nothing unit that no task can land without landing the others, and this plan's tasks each have to leave the suite green. So the six steps become four files, applied in order: `007_vocabulary.sql` (enum renames, the jsonb rewrite, `capture_settings` -> `record_settings`), `008_events.sql` (`events`, `entry_events`), `009_extract_jobs.sql` (`job_status`, `extract_jobs`), `010_retire_capture_jobs.sql` (`capture_jobs` -> `capture_jobs_legacy`). Nothing is lost: `migrate()` applies every pending file inside one transaction, so a fresh install still gets all-or-nothing, and a partially-built branch cannot leave a half-migrated database behind. **Update the spec's "Migration from capture" heading in Task 1** so the two documents agree.

**2. `extract_jobs` carries `covers_through timestamptz`.** The spec defines "already extracted" nowhere precisely, and the obvious reading - "the session has a done job" - is wrong for a resumed session: events recorded after that job would be counted as extracted and become eligible for prune having produced nothing. `covers_through` is the newest `occurred_at` among the events a job actually read. An event is extracted when a `done` job for its session has `covers_through >= event.occurred_at`, and a session is awaiting extraction when it holds events past every done job's watermark. One column removes the whole class of bug.

**3. The idle window is `REMEM_IDLE_MINUTES`, default 20**, resolved by `config.load()` like every other setting, and clamped positive. A zero or negative window would make every session extractable the instant its first event landed, which is extraction racing the session that is still writing.

**4. The harness's own parsing is an adapter capability, `event()`, probed with `getattr`.** Same contract as `env_settings()`/`settings_path()`: an adapter without it records nothing and says so, an adapter whose `event()` raises degrades to a warning, and a broken third-party adapter is never why `remem record event` will not run. This is what keeps `services/record.py` from growing a `if harness == "claude-code"` branch when Cursor lands.

---

## File Structure

**Created:**
- `src/remem/backends/postgres/migrations/007_vocabulary.sql` - the renames and the jsonb rewrite.
- `src/remem/backends/postgres/migrations/008_events.sql` - `event_kind`, `events`, `entry_events`.
- `src/remem/backends/postgres/migrations/009_extract_jobs.sql` - `job_status`, `extract_jobs`.
- `src/remem/backends/postgres/migrations/010_retire_capture_jobs.sql` - the dead spool, renamed and left alone.
- `src/remem/services/record.py` - the opt-in gate and the single INSERT. Every decision about what gets recorded.
- `src/remem/services/extraction.py` - the idle rule, job claiming, the extractor call, provenance writes. Replaces `services/capture.py`.
- `src/remem/services/events.py` - prune policy, window parsing, status counts, forensic lookup.
- `src/remem/extract/__init__.py`, `src/remem/extract/base.py`, `src/remem/extract/claude_cli.py` - the `distill` package renamed, with events rather than a transcript as input.
- Tests: `tests/test_vocabulary_migration.py`, `tests/test_stale_vectors.py`, `tests/test_events_schema.py`, `tests/test_store_events.py`, `tests/test_record_service.py`, `tests/test_record_cli.py`, `tests/test_record_hook.py`, `tests/test_extract_jobs_store.py`, `tests/test_extraction_service.py`, `tests/test_events_process_cli.py`, `tests/test_events_prune.py`, `tests/test_record_status.py`, `tests/test_claude_code_events_install.py`, `tests/test_extract_names.py`.

**Modified:**
- `src/remem/domain.py` - `Kind.NOTE`, `Origin.EXTRACTED`, `EventKind`, `JobStatus`, `Event`, `ExtractJob`, `SessionRef`.
- `src/remem/store.py` - the recording, event, provenance, job, prune and lock operations.
- `src/remem/backends/postgres/store.py` - all of their SQL.
- `src/remem/backends/postgres/migrate.py` - `_migration_files` becomes public `migration_files`.
- `src/remem/services/search.py` - `DEFAULT_ORIGINS` gains `EXTRACTED`; the embedder probe moves inside the try.
- `src/remem/services/write.py` - default kind; stale vectors dropped on update.
- `src/remem/services/kb.py`, `src/remem/services/handoff.py` - origin lists.
- `src/remem/cli.py` - the `record` and `events` command groups; `capture` becomes a hidden warning alias.
- `src/remem/mcp_server.py` - kind vocabulary in the tool docstrings.
- `src/remem/config.py` - `extract_model` (with the old env name as a warning fallback), `idle_minutes`.
- `src/remem/agents/base.py` - `HarnessEvent` and the documented `event()` capability.
- `src/remem/agents/claude_code/adapter.py` - `event()`, the new hook registrations, the install round-trip.
- `src/remem/agents/claude_code/hook.py` - `record_event`; `session_end` stops enqueueing capture.
- `src/remem/agents/claude_code/env_vars.py` - the renamed and new variables.
- `README.md`, `CLAUDE.md` - the pipeline and the vocabulary.
- Existing `tests/test_capture_*.py` are renamed to their `record`/`extract` equivalents as their subject moves.

**Deleted:**
- `src/remem/services/capture.py` (becomes `services/extraction.py` in Task 6).
- `src/remem/distill/` (becomes `src/remem/extract/` in Task 6).

---

### Task 1: The vocabulary, in the schema and in the code

The spec's terminology table is binding on schema, code and CLI, and a half-applied rename is worse than none: the code would read in two languages and every later task would have to guess which one it is in. So the rename lands first, whole, and by itself.

Two of its steps are dangerous and neither looks it:

- **`collections.query` is `jsonb`.** `ALTER TYPE ... RENAME VALUE` renames the enum label; it does not reach the literal string `"memory"` stored inside a smart collection's query. Without a data rewrite, every collection filtering on that kind matches nothing after the migration - and "an empty query matches nothing, forever" is this codebase's own documented sharp edge. This is the highest-risk line in the plan.
- **`capture_settings` is a live table** holding the per-project opt-in, which is the entire privacy story. It is named in string SQL in three places where no type checker will catch a miss. A missed call site does not error usefully: the opt-in lookup fails, and a fail-soft hook then records nothing, silently, forever.

`ALTER TYPE ... RENAME VALUE` is safe inside `migrate()`'s caller-owned transaction, unlike the famous `ADD VALUE`. It rewrites no rows: the label changes, the ordinal does not.

**Files:**
- Create: `src/remem/backends/postgres/migrations/007_vocabulary.sql`
- Create: `tests/test_vocabulary_migration.py`
- Modify: `src/remem/domain.py:11-14` (`Kind`), `:22-27` (`Origin`)
- Modify: `src/remem/backends/postgres/migrate.py:18-26` (`_migration_files` -> `migration_files`), and its two callers at `:40` and `:55`
- Modify: `src/remem/backends/postgres/store.py:574-596` (`set_capture_enabled`, `capture_enabled`), `:733-741` (`enabled_capture_projects`)
- Modify: `src/remem/store.py:58-60` (the three Protocol lines)
- Modify: `src/remem/services/search.py:29` (`DEFAULT_ORIGINS`), `src/remem/services/capture.py:136,158`, `src/remem/services/write.py:39`, `src/remem/cli.py:182`, `src/remem/mcp_server.py:57,71,104`, `src/remem/distill/base.py:82`
- Modify: `docs/superpowers/specs/2026-08-28-events-and-recall-design.md` (the "Migration from capture" opening paragraph)
- Test: `tests/test_vocabulary_migration.py`, plus updates to every existing test naming `memory` or `capture_settings`

**Interfaces:**
- Consumes: nothing.
- Produces: `Kind.NOTE = "note"` (replacing `Kind.MEMORY`), `Origin.EXTRACTED = "extracted"` (replacing `Origin.CAPTURE`), `Store.set_record_enabled(owner_id, project, enabled) -> None`, `Store.record_enabled(owner_id, project) -> bool`, `Store.enabled_record_projects(owner_id) -> list[str]`, and `migrate.migration_files() -> list[tuple[str, str]]`. Tasks 4, 5, 6, 8 use all of these.

- [ ] **Step 1: Write the failing test**

Create `tests/test_vocabulary_migration.py`:

```python
"""The renames, and the one they do not reach.

`ALTER TYPE ... RENAME VALUE` renames an enum label. It does not touch the
string "memory" sitting inside a smart collection's jsonb query, and a
collection whose query matches nothing is this codebase's documented worst
failure - silent, permanent, and indistinguishable from an empty store. So
the interesting test here is not "does the enum say note", it is "does a
collection created BEFORE the migration still resolve to the same entries
after it".
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from remem.backends.postgres.migrate import migration_files
from remem.backends.postgres.store import PostgresStore
from remem.domain import Kind, Origin
from remem.services import kb

pytestmark = pytest.mark.db

BEFORE = "006_vectors"


def apply_through(conn, last_version: str) -> None:
    """Apply migrations up to and including `last_version`.

    The suite's other database tests start from a fully migrated schema.
    This one has to stand in the middle, because the whole question is what
    happens to rows written under the old vocabulary.
    """
    conn.execute(
        "create table if not exists schema_migrations ("
        " version text primary key,"
        " applied_at timestamptz not null default clock_timestamp())"
    )
    for version, sql in migration_files():
        conn.execute(sql)
        conn.execute(
            "insert into schema_migrations (version) values (%s)", (version,)
        )
        if version == last_version:
            return


def apply_rest(conn) -> None:
    done = {
        r[0]
        for r in conn.execute("select version from schema_migrations").fetchall()
    }
    for version, sql in migration_files():
        if version in done:
            continue
        conn.execute(sql)
        conn.execute(
            "insert into schema_migrations (version) values (%s)", (version,)
        )


def _old_world(conn):
    """A principal, an old-vocabulary entry, and a collection that finds it."""
    owner = uuid4()
    conn.execute(
        "insert into principals (id, handle, kind) values (%s, 'pre', 'user')",
        (owner,),
    )
    entry = uuid4()
    conn.execute(
        "insert into entries (id, kind, title, body, owner_id, project, origin)"
        " values (%s, 'memory', 'old', 'body', %s, 'proj', 'capture')",
        (entry, owner),
    )
    conn.execute(
        "insert into collections (id, slug, title, owner_id, query)"
        " values (%s, 'pre', 'Pre', %s, %s)",
        (uuid4(), owner, json.dumps(
            {"tags": [], "kinds": ["memory"], "project": "proj"}
        )),
    )
    return owner, entry


def test_a_pre_migration_collection_resolves_to_the_same_entries(conn):
    apply_through(conn, BEFORE)
    owner, entry = _old_world(conn)

    apply_rest(conn)

    resolved = kb.resolve(PostgresStore(conn), owner, "pre")
    assert [e.id for e in resolved] == [entry]
    assert resolved[0].kind is Kind.NOTE


def test_a_captured_entry_reads_back_as_extracted(conn):
    apply_through(conn, BEFORE)
    owner, entry = _old_world(conn)

    apply_rest(conn)

    stored = PostgresStore(conn).get_entry(entry, owner)
    assert stored is not None
    assert stored.origin is Origin.EXTRACTED


def test_the_opt_in_survives_the_table_rename(conn):
    apply_through(conn, BEFORE)
    owner = uuid4()
    conn.execute(
        "insert into principals (id, handle, kind) values (%s, 'optin', 'user')",
        (owner,),
    )
    conn.execute(
        "insert into capture_settings (owner_id, project, enabled)"
        " values (%s, 'proj', true)",
        (owner,),
    )

    apply_rest(conn)

    store = PostgresStore(conn)
    assert store.record_enabled(owner, "proj") is True
    assert store.enabled_record_projects(owner) == ["proj"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_vocabulary_migration.py -v`
Expected: FAIL - `ImportError: cannot import name 'migration_files'`.

- [ ] **Step 3: Make `migration_files` public**

In `src/remem/backends/postgres/migrate.py`, rename `_migration_files` to `migration_files`, update the two internal callers (`pending_versions`, `migrate`), and add one line to its docstring:

```python
def migration_files() -> list[tuple[str, str]]:
    """(version, sql) pairs sorted by filename.

    Public because a migration whose job is to rewrite existing data can
    only be tested by standing in the middle of the sequence - applying up
    to the version before it, writing rows the old way, and then applying
    the rest.
    """
```

- [ ] **Step 4: Write the migration**

Create `src/remem/backends/postgres/migrations/007_vocabulary.sql`:

```sql
-- The vocabulary the events design settled: 'memory' becomes 'note',
-- 'capture' becomes 'extracted', and capture_settings becomes
-- record_settings. Names only - no table gains or loses a column here.
--
-- ALTER TYPE ... RENAME VALUE is transactional, unlike ADD VALUE, so this
-- runs safely inside migrate()'s caller-owned transaction. It rewrites no
-- rows: the label changes, the ordinal does not, and every existing row
-- reads back under the new name for free.

alter type entry_kind rename value 'memory' to 'note';
alter type entry_origin rename value 'capture' to 'extracted';

-- THE GAP THE RENAME DOES NOT CLOSE.
--
-- collections.query is jsonb. A smart collection filtering on kinds stores
-- the literal string "memory" inside that JSON, and renaming the enum label
-- does not reach inside it. Left alone, every such collection would match
-- nothing after this migration - and an empty query matching nothing,
-- forever, is precisely this codebase's documented sharp edge, arrived at
-- once already by accident. It is also the one line a reader would never
-- think to look for, which is why it is spelled out rather than folded in.
update collections
   set query = jsonb_set(
         query,
         '{kinds}',
         (select coalesce(
                   jsonb_agg(
                     case when k = '"memory"'::jsonb
                          then '"note"'::jsonb
                          else k end
                   ),
                   '[]'::jsonb)
            from jsonb_array_elements(query -> 'kinds') as k)
       )
 where query ? 'kinds'
   and query -> 'kinds' @> '["memory"]'::jsonb;

-- A live table, not a dead one: record_settings holds the per-project
-- opt-in, which is the entire privacy story of the events pipeline and is
-- read on the path of every recorded event. The shape is unchanged, so this
-- is a bare rename with nothing to get wrong in SQL. The risk is a PARTIAL
-- rename - three statements in backends/postgres/store.py name this table
-- in string SQL, where no type checker will catch a miss, and a missed one
-- fails the opt-in lookup rather than erroring. A fail-soft hook then
-- records nothing, silently. All three move in the same commit as this file.
alter table capture_settings rename to record_settings;
```

- [ ] **Step 5: Rename the domain values**

In `src/remem/domain.py`:

```python
class Kind(StrEnum):
    NOTE = "note"
    DOC = "doc"
    RULE = "rule"


class Origin(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    #: Written by the extractor from a session's events. Was 'capture'.
    EXTRACTED = "extracted"
    HANDOFF = "handoff"
```

No compatibility aliases. `Kind.MEMORY` must fail loudly at attribute access; an alias would let a frontend keep writing the old label past the point where the database stopped accepting it.

- [ ] **Step 6: Rename the three store call sites and the Protocol**

In `src/remem/backends/postgres/store.py`, rename `set_capture_enabled` -> `set_record_enabled`, `capture_enabled` -> `record_enabled`, `enabled_capture_projects` -> `enabled_record_projects`, and change `capture_settings` to `record_settings` in all three bodies. Update the matching three lines in `src/remem/store.py`. Leave every `capture_jobs` method alone - that table is retired in Task 6, not here.

- [ ] **Step 7: Follow the renames through the rest of the tree**

Mechanical, and the list is exhaustive:

- `src/remem/services/search.py:29` - `DEFAULT_ORIGINS = [Origin.HUMAN, Origin.AGENT, Origin.EXTRACTED]`
- `src/remem/services/capture.py:136,158` - `Origin.EXTRACTED`
- `src/remem/services/write.py:39` - `kind: Kind = Kind.NOTE`
- `src/remem/cli.py:182` - `= Kind.NOTE`
- `src/remem/mcp_server.py:57,71,104` - `"note"` in the default and both docstrings
- `src/remem/distill/base.py:82` - `Kind(item.get("kind", "note"))`, and the prompt in `src/remem/distill/claude_cli.py` which offers the model `"memory|doc|rule"` becomes `"note|doc|rule"`
- Every existing test that writes `Kind.MEMORY`, `"memory"`, `Origin.CAPTURE` or `capture_settings`

- [ ] **Step 8: Run the whole suite**

Run: `docker compose ps && uv run pytest`
Expected: 546 passed, 0 skipped (543 baseline plus this task's three).

- [ ] **Step 9: Correct the spec's migration numbering**

In `docs/superpowers/specs/2026-08-28-events-and-recall-design.md`, replace the "One migration, `007_events.sql` - `006` is the semantic-recall migration, which shipped first." sentence with a pointer to the four files this plan lands, naming each one's step. The spec is the document a future reader reaches for first; leaving it describing a file that does not exist makes the schema history unreadable.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "Rename the vocabulary: note, extracted, record_settings"
```

---

### Task 2: The two follow-ups carried over from semantic recall

Two known defects, both small, both in code this plan is about to build on. They land here rather than at the end because the end is where carried-over work goes to die, and the second one is a crash in a path Task 6 will start exercising much harder.

**F1: the embedder's dimension probe sits outside the try.** `services/search.shared_embedder` catches `EmbedderUnavailable`, but constructing a `LocalEmbedder` runs a real inference call to learn its dimension. An ONNX failure there raises something else entirely, escapes `_semantic`, and crashes the whole search instead of degrading to two tiers - while `_semantic`'s docstring claims the opposite.

**F2: an edited entry keeps its stale vector.** `services/write.update` mutates in place, so `entries_missing_vectors` never offers that entry again for that model. The entry stays findable by its OLD wording while returning its NEW text, and the match marker cannot warn about it: by the semantic tier's own rules it is a legitimate hit. The fix belongs to this plan because Task 6 makes entry writes far more frequent, and because `put_entry` is being touched anyway.

**Files:**
- Modify: `src/remem/services/search.py:60-77` (`shared_embedder`)
- Modify: `src/remem/backends/postgres/store.py:201-248` (`put_entry`)
- Test: `tests/test_stale_vectors.py`, `tests/test_services_search.py`

**Interfaces:**
- Consumes: `Store.put_vector`, `Store.entries_missing_vectors` (both shipped).
- Produces: no new names. `put_entry` gains the documented behaviour that updating an entry's title or body deletes its vectors.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stale_vectors.py`:

```python
"""An edited entry must not stay findable by its old wording.

This one cannot be caught by a marker. A semantic hit on a stale vector is a
legitimate hit by the tier's own rules - the vector really is close to the
query - so nothing downstream can warn about it. The only place to fix it is
at the write, by deleting the derived row the moment its source changes.
"""

from __future__ import annotations

import pytest

from remem.domain import Query
from remem.services import write

pytestmark = pytest.mark.db


def test_editing_the_body_drops_the_vector(store, owner):
    entry = write.remember(
        store, owner.id, title="pgvector indexing", body="original text"
    )
    store.put_vector(entry.id, "m", 3, [1.0, 0.0, 0.0], owner.id)

    write.update(store, owner.id, entry.id, body="entirely different text")

    missing = store.entries_missing_vectors(owner.id, "m", limit=10)
    assert [e.id for e in missing] == [entry.id]


def test_a_write_that_changes_no_text_keeps_the_vector(store, owner):
    """Re-embedding on every touch would make `remem embed` never finish.

    Linking two entries calls put_entry, and so does superseding. Neither
    changes what the entry says, so neither invalidates what it means.
    """
    entry = write.remember(store, owner.id, title="t", body="b")
    store.put_vector(entry.id, "m", 3, [1.0, 0.0, 0.0], owner.id)

    write.update(store, owner.id, entry.id, tags=["new-tag"])

    assert store.entries_missing_vectors(owner.id, "m", limit=10) == []


def test_the_semantic_tier_cannot_return_the_stale_wording(store, owner):
    """The end-to-end version of the first test, stated as search behaviour."""
    entry = write.remember(store, owner.id, title="t", body="original")
    store.put_vector(entry.id, "m", 3, [1.0, 0.0, 0.0], owner.id)
    write.update(store, owner.id, entry.id, body="different")

    hits = store.semantic_search(
        Query(text="original", limit=10), owner.id,
        [1.0, 0.0, 0.0], "m", 0.5,
    )
    assert hits == []
```

Add to `tests/test_services_search.py`:

```python
def test_a_raising_embedder_constructor_costs_the_tier_and_not_the_search(
    store, owner, monkeypatch
):
    """The probe is a real inference call, and it is outside the old try.

    shared_embedder caught EmbedderUnavailable only. A LocalEmbedder that
    imports fine and then fails while measuring its own dimension raises
    something else, which escaped the tier and crashed the search - the
    exact opposite of what _semantic's docstring promises.
    """
    monkeypatch.setattr("remem.services.search._EMBEDDERS", {})

    def explode(name):
        raise RuntimeError("onnxruntime session failed")

    monkeypatch.setattr("remem.services.search.load_embedder", explode)

    write.remember(store, owner.id, title="config command", body="body")
    hits = search.find(store, owner.id, Query(text="zzzz nothing", limit=5))
    assert hits == []  # degraded to two tiers, did not raise
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_stale_vectors.py tests/test_services_search.py -v`
Expected: the three stale-vector tests fail (the entry is not re-offered), and the embedder test fails with `RuntimeError: onnxruntime session failed`.

- [ ] **Step 3: Widen the embedder guard**

In `src/remem/services/search.py`:

```python
    try:
        embedder: Embedder | None = load_embedder(model_name)
    except EmbedderUnavailable:
        embedder = None
    except Exception:
        # Deliberately broader than the declared exception. Constructing a
        # LocalEmbedder does not only import fastembed - it builds an ONNX
        # session and runs one real inference call to learn its dimension,
        # and that call fails in ways the embed layer never promised to
        # wrap: a corrupt download, a missing shared library, an OOM. Every
        # one of them means the same thing here, which is what this
        # function exists to say: no semantic tier, carry on with two.
        embedder = None
```

- [ ] **Step 4: Drop stale vectors at the write**

In `backends/postgres/store.py`, inside `put_entry`, after the upsert, delete this entry's vectors when its text changed. Compare against the pre-write row rather than re-embedding on every touch:

```python
            # Derived data must never outlive the text it was derived from.
            # An edited entry whose vector survives stays findable by its OLD
            # wording while returning its NEW body, and nothing downstream can
            # warn about it: the semantic tier's marker says "semantic", which
            # is true - the vector really is near the query. Deleting here
            # makes entries_missing_vectors offer the row again, so `remem
            # embed` repairs it on its next run.
            #
            # Only a text change counts. Linking, tagging and superseding all
            # go through put_entry too, and re-embedding on those would give
            # `remem embed` a backlog that never empties.
            if text_changed:
                cur.execute(
                    "delete from entry_vectors where entry_id = %s",
                    (entry.id,),
                )
```

Read the previous `title` and `body` in the same cursor immediately before the upsert to compute `text_changed`; a row that did not exist counts as changed and deletes nothing.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_stale_vectors.py tests/test_services_search.py tests/test_semantic_search.py tests/test_embed_service.py -v`
Expected: PASS.

- [ ] **Step 6: Record the follow-ups as closed**

In `docs/superpowers/plans/2026-08-28-semantic-recall.md`, mark F1 and the stale-vector item in "Follow-ups from execution" as done, naming this plan's Task 2. An open follow-up list that never closes stops being read.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Close the two follow-ups semantic recall left open"
```

---

### Task 3: `events` and `entry_events`

The raw substrate and its provenance. Both tables land together because `entry_events` without `events` records where an entry came from without there being anywhere for it to have come from, and neither is testable alone.

Two shapes here look like mistakes and are not:

- **`entry_events` has no foreign key to `events`.** A foreign key would force a choice between blocking prune and erasing provenance, and both are worse than a recorded pointer to something we deliberately deleted. The row is an audit record - "this entry came from event X, in session Y, on harness Z" - and that stays true after event X is gone. `session_id` and `harness` are denormalised onto it for exactly that reason.
- **`payload` is stored whole.** Truncating at record time is lossy forever and degrades the extraction this table exists to enable. The mitigations are the opt-in gate and an explicit prune, not partial capture.

**Files:**
- Create: `src/remem/backends/postgres/migrations/008_events.sql`
- Create: `tests/test_events_schema.py`, `tests/test_store_events.py`
- Modify: `src/remem/domain.py` (append `EventKind`, `Event`, `SessionRef`)
- Modify: `src/remem/store.py` (the events section of the Protocol)
- Modify: `src/remem/backends/postgres/store.py` (a new `# ---------------- events ----------------` section)

**Interfaces:**
- Consumes: `Kind`, `Origin` from Task 1.
- Produces, all used by Tasks 4 and 6:
  - `EventKind(StrEnum)`: `TOOL_CALL = "tool_call"`, `MESSAGE = "message"`, `SESSION_END = "session_end"`.
  - `Event` dataclass: `id: UUID`, `owner_id: UUID`, `project: str`, `harness: str`, `session_id: str`, `kind: EventKind`, `payload: dict`, `tool: str | None = None`, `occurred_at: datetime | None = None`, `recorded_at: datetime | None = None`.
  - `SessionRef` dataclass: `project: str`, `harness: str`, `session_id: str`, `event_count: int`, `last_event_at: datetime`, `extract_from: datetime | None` - the watermark past which this session's events are unextracted, `None` meaning "all of them".
  - `Store.put_event(event: Event) -> Event`
  - `Store.events_for_session(owner_id: UUID, project: str, harness: str, session_id: str, since: datetime | None = None, limit: int = 500) -> list[Event]` - oldest first.
  - `Store.link_entry_events(entry_id: UUID, events: list[Event], owner_id: UUID) -> None`
  - `Store.provenance(entry_id: UUID, owner_id: UUID) -> list[tuple[UUID, str, str, bool]]` - `(event_id, session_id, harness, event_still_present)`.

- [ ] **Step 1: Write the failing schema test**

Create `tests/test_events_schema.py`:

```python
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_events_schema.py -v`
Expected: FAIL - `to_regclass('public.events')` returns None.

- [ ] **Step 3: Write the migration**

Create `src/remem/backends/postgres/migrations/008_events.sql`:

```sql
-- Raw material, append-only. Everything downstream is derived from this and
-- recomputable from it; nothing here is derived from anything.

create type event_kind as enum ('tool_call','message','session_end');

create table events (
  id            uuid primary key,
  owner_id      uuid not null references principals(id),
  project       text not null,
  harness       text not null,          -- 'claude-code' | 'cursor' | 'opencode'
  session_id    text not null,
  kind          event_kind not null,
  tool          text,                   -- null for non-tool events
  payload       jsonb not null,
  occurred_at   timestamptz not null,
  recorded_at   timestamptz not null default clock_timestamp()
);

-- kind is small and closed; everything harness-specific stays in payload,
-- unparsed. A harness changing its payload shape must not be able to break
-- the write path - parsing is the extractor's problem, and the extractor can
-- be fixed and re-run because the raw is still here.
--
-- Payloads are stored IN FULL. Truncating at record time is lossy forever
-- and would degrade the extraction this table exists to enable. The
-- mitigation is the opt-in gate in services/record.py and an explicit prune
-- the user reaches for - not partial capture, and not automatic expiry.

-- The two queries that matter: a session's events in order (extraction), and
-- the newest event per session (the idle trigger).
create index events_session_idx
  on events (owner_id, project, harness, session_id, occurred_at);

create table entry_events (
  entry_id   uuid not null references entries(id) on delete cascade,
  event_id   uuid,                      -- deliberately no FK, see below
  session_id text not null,
  harness    text not null,
  primary key (entry_id, event_id)
);

-- NO FOREIGN KEY ON event_id, DELIBERATELY.
--
-- This row is an audit record, not a live relationship. "This entry came
-- from event X, in session Y, on harness Z" is a fact about the past, and it
-- stays true after event X is pruned. A foreign key would model it as though
-- it had stopped being true, which is the wrong claim - and would force
-- prune to choose between blocking and erasing provenance, both worse than a
-- pointer to something we deleted on purpose. session_id and harness are
-- denormalised here for the same reason: they are what survives the prune.
--
-- Two rules follow, and they are load-bearing:
--   1. NO READ PATH MAY DEREFERENCE event_id. The only query allowed to
--      follow it is forensic - show me the raw behind this entry, if we
--      still have it - and that query treats absence as an ordinary answer.
--   2. DANGLING MUST BE VISIBLE, NEVER INFERRED. prune reports how many
--      provenance rows it just left dangling, and the forensic lookup says
--      "event pruned" rather than "not found". Silence about deleted raw is
--      how a user concludes provenance was never recorded at all.
--
-- Ids are uuid7, so a dangling event_id cannot later be reused by a
-- different event and quietly acquire a wrong meaning.

create index entry_events_event_idx on entry_events (event_id);
```

- [ ] **Step 4: Run the schema test**

Run: `uv run pytest tests/test_events_schema.py -v`
Expected: PASS.

- [ ] **Step 5: Write the failing store test**

Create `tests/test_store_events.py`:

```python
"""Reading and writing raw events, and the provenance beside them."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Event, EventKind, new_id
from remem.services import write
from remem.store import NotOwner

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


def test_an_event_round_trips_with_its_payload_whole(store, owner):
    big = {"output": "x" * 50_000, "nested": {"a": [1, 2, 3]}}
    stored = store.put_event(an_event(owner, payload=big))

    got = store.events_for_session(owner.id, "remem", "claude-code", "s1")
    assert [e.id for e in got] == [stored.id]
    assert got[0].payload == big
    assert got[0].tool == "Bash"
    assert got[0].kind is EventKind.TOOL_CALL


def test_events_come_back_oldest_first(store, owner):
    later = store.put_event(an_event(owner, at=NOW + timedelta(minutes=5)))
    earlier = store.put_event(an_event(owner, at=NOW))

    got = store.events_for_session(owner.id, "remem", "claude-code", "s1")
    assert [e.id for e in got] == [earlier.id, later.id]


def test_since_excludes_events_at_or_before_the_watermark(store, owner):
    store.put_event(an_event(owner, at=NOW))
    after = store.put_event(an_event(owner, at=NOW + timedelta(minutes=5)))

    got = store.events_for_session(
        owner.id, "remem", "claude-code", "s1", since=NOW
    )
    assert [e.id for e in got] == [after.id]


def test_another_principals_events_are_invisible(store, owner):
    store.put_event(an_event(owner))
    other = store.ensure_principal("someone-else")
    assert store.events_for_session(
        other.id, "remem", "claude-code", "s1"
    ) == []


def test_provenance_survives_the_events_it_names(store, owner):
    entry = write.remember(store, owner.id, title="t", body="b")
    event = store.put_event(an_event(owner))
    store.link_entry_events(entry.id, [event], owner.id)

    rows = store.provenance(entry.id, owner.id)
    assert rows == [(event.id, "s1", "claude-code", True)]

    store.prune_events(owner.id, before=NOW + timedelta(days=1), force=True)

    rows = store.provenance(entry.id, owner.id)
    assert rows == [(event.id, "s1", "claude-code", False)]


def test_provenance_for_another_owners_entry_raises(store, owner):
    entry = write.remember(store, owner.id, title="t", body="b")
    event = store.put_event(an_event(owner))
    other = store.ensure_principal("someone-else")
    with pytest.raises(NotOwner):
        store.link_entry_events(entry.id, [event], other.id)
```

- [ ] **Step 6: Run it to verify it fails**

Run: `uv run pytest tests/test_store_events.py -v`
Expected: FAIL - `ImportError: cannot import name 'Event' from 'remem.domain'`.

- [ ] **Step 7: Add the domain types**

Append to `src/remem/domain.py`:

```python
class EventKind(StrEnum):
    """What a harness handed us. Small and closed on purpose.

    Everything harness-specific lives in the payload, unparsed: a harness
    that changes its payload shape must not be able to break the write path.
    A fourth value is deliberately deferred until something writes one -
    adding an enum value later is cheap, and guessing now invites a label
    nothing ever produces.
    """

    TOOL_CALL = "tool_call"
    MESSAGE = "message"
    SESSION_END = "session_end"


@dataclass(slots=True)
class Event:
    """One thing that happened, as raw as it reached us."""

    id: UUID
    owner_id: UUID
    project: str
    harness: str
    session_id: str
    kind: EventKind
    payload: dict
    tool: str | None = None
    occurred_at: datetime | None = None
    recorded_at: datetime | None = None


@dataclass(slots=True)
class SessionRef:
    """A session with events, as the idle trigger sees it.

    `extract_from` is the watermark: the newest `covers_through` of a done
    extract job for this session, or None when nothing has ever extracted
    it. Events at or before it have already produced whatever they were
    going to produce.
    """

    project: str
    harness: str
    session_id: str
    event_count: int
    last_event_at: datetime
    extract_from: datetime | None = None
```

- [ ] **Step 8: Add the Protocol methods**

In `src/remem/store.py`, under a new `# events` heading, add `put_event`, `events_for_session`, `link_entry_events`, `provenance` with the signatures in this task's Interfaces block. `prune_events` and the job methods arrive in Tasks 5 and 7; do not stub them here - a Protocol method with no implementation is a lie the type checker will not catch.

- [ ] **Step 9: Implement them**

In `backends/postgres/store.py`, add an events section. Points worth getting right:

```python
    def put_event(self, event: Event) -> Event:
        """One INSERT. This is the hot path - it runs per tool call."""
        with self._cur() as cur:
            cur.execute(
                """
                insert into events (
                  id, owner_id, project, harness, session_id,
                  kind, tool, payload, occurred_at
                ) values (
                  %(id)s, %(owner_id)s, %(project)s, %(harness)s,
                  %(session_id)s, %(kind)s::event_kind, %(tool)s,
                  %(payload)s::jsonb, %(occurred_at)s
                )
                returning recorded_at
                """,
                {..., "payload": json.dumps(event.payload),
                 "occurred_at": event.occurred_at or datetime.now(timezone.utc)},
            )
            event.recorded_at = cur.fetchone()["recorded_at"]
        return event

    def link_entry_events(self, entry_id, events, owner_id) -> None:
        # Ownership is checked here, like every other write: the entry must
        # belong to this principal before we record anything about where it
        # came from.
        ...
        # on conflict (entry_id, event_id) do nothing - re-running an
        # extraction must not fail on provenance it already wrote.

    def provenance(self, entry_id, owner_id):
        """The forensic lookup, and the only query allowed to follow event_id.

        A left join, never an inner one: a pruned event must come back as a
        row with `present = False`, because "we recorded where this came from
        and then deleted the raw" and "we never recorded anything" are
        different answers and the user needs to be able to tell them apart.
        """
```

`occurred_at` defaulting to now is a service concern in principle, but it lands here because a NOT NULL column with no default is how a fail-soft hook turns into a lost event.

- [ ] **Step 10: Run the tests**

Run: `uv run pytest tests/test_store_events.py tests/test_events_schema.py -v`
Expected: PASS, except `test_provenance_survives_the_events_it_names`, which needs `prune_events` from Task 7. Mark it `@pytest.mark.xfail(reason="prune_events lands in Task 7", strict=True)` for now and remove the marker there - Task 7's step list says to.

- [ ] **Step 11: Run the whole suite and commit**

```bash
docker compose ps && uv run pytest
git add -A
git commit -m "Store raw events and the provenance beside them"
```

---

### Task 4: `remem record event`, and the gate in front of it

The write path. One INSERT, fail-soft, behind the per-project opt-in that is the entire safety story of this design - and which is checked in the service, never in the frontend.

The adapter parses; the service decides. `services/record.py` never learns what a Claude Code payload looks like, because the moment it does, Cursor's arrival adds a second branch to it and the seam is gone.

**Files:**
- Create: `src/remem/services/record.py`
- Create: `tests/test_record_service.py`, `tests/test_record_cli.py`
- Modify: `src/remem/agents/base.py` (append `HarnessEvent`, document the `event()` capability)
- Modify: `src/remem/agents/claude_code/adapter.py` (implement `event()`)
- Modify: `src/remem/cli.py` (a `record` command group)
- Modify: `tests/test_capture_service.py` - move its opt-in tests into `tests/test_record_service.py` and leave the drain tests where they are; Task 6 deletes what remains

**Interfaces:**
- Consumes: `Store.put_event`, `Store.record_enabled`, `Event`, `EventKind` (Tasks 1 and 3).
- Produces:
  - `agents.base.HarnessEvent` dataclass: `kind: EventKind`, `session_id: str`, `project: str | None`, `tool: str | None = None`, `payload: dict = field(default_factory=dict)`, `occurred_at: datetime | None = None`.
  - `ClaudeCodeAdapter.event(env: Mapping[str, str], payload: dict) -> HarnessEvent | None`.
  - `services.record.enable/disable/is_enabled(store, owner_id, project)`.
  - `services.record.record(store, owner_id, harness_event: HarnessEvent, harness: str) -> Event | None` - `None` means the gate is closed.
  - `services.record.RecordingDisabled` - not raised on the hook path; used by `remem record event` to explain under `--json`.
  - CLI: `remem record event [--agent claude-code]` reading JSON on stdin, `remem record enable|disable [--project]`.

- [ ] **Step 1: Write the failing service test**

Create `tests/test_record_service.py`:

```python
"""The opt-in gate, and the single INSERT behind it.

The gate is checked HERE and nowhere else. Every frontend - the hook, the
CLI, and whatever a third-party adapter does - gets it for free, and the one
place to look when asking "could this project have recorded anything" is
this module.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from remem.agents.base import HarnessEvent
from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import EventKind
from remem.services import record

pytestmark = pytest.mark.db

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def a_harness_event(**kw):
    return HarnessEvent(
        kind=kw.get("kind", EventKind.TOOL_CALL),
        session_id=kw.get("session_id", "s1"),
        project=kw.get("project", "remem"),
        tool=kw.get("tool", "Bash"),
        payload=kw.get("payload", {"command": "ls"}),
        occurred_at=kw.get("occurred_at", NOW),
    )


def test_nothing_is_recorded_for_a_project_that_did_not_opt_in(store, owner):
    assert record.record(store, owner.id, a_harness_event(), "claude-code") is None
    assert store.events_for_session(
        owner.id, "remem", "claude-code", "s1"
    ) == []


def test_an_opted_in_project_records(store, owner):
    record.enable(store, owner.id, "remem")

    event = record.record(store, owner.id, a_harness_event(), "claude-code")

    assert event is not None
    stored = store.events_for_session(owner.id, "remem", "claude-code", "s1")
    assert [e.id for e in stored] == [event.id]
    assert stored[0].harness == "claude-code"


def test_disable_closes_the_gate_again(store, owner):
    record.enable(store, owner.id, "remem")
    record.disable(store, owner.id, "remem")
    assert record.record(store, owner.id, a_harness_event(), "claude-code") is None


def test_an_event_with_no_project_is_refused(store, owner):
    """A project-less event cannot be gated, so it must not be recorded.

    The opt-in is per project. An event that does not know which project it
    belongs to would have to be either recorded unconditionally - defeating
    the gate - or attributed to a guess. Refusing is the only honest answer.
    """
    record.enable(store, owner.id, "remem")
    assert record.record(
        store, owner.id, a_harness_event(project=None), "claude-code"
    ) is None


def test_the_opt_in_is_per_project_not_global(store, owner):
    record.enable(store, owner.id, "remem")
    assert record.record(
        store, owner.id, a_harness_event(project="other"), "claude-code"
    ) is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_record_service.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'remem.services.record'`.

- [ ] **Step 3: Add `HarnessEvent` and document the capability**

In `src/remem/agents/base.py`, after `Identity`:

```python
@dataclass(slots=True)
class HarnessEvent:
    """One event, as an adapter read it out of its harness's payload.

    Deliberately not an `Event`: the domain type carries an id, an owner and
    a harness name, none of which an adapter is entitled to decide. The
    adapter answers what happened and when; the service answers whether it
    may be recorded and under whose name.
    """

    kind: EventKind
    session_id: str
    project: str | None
    tool: str | None = None
    payload: dict = field(default_factory=dict)
    occurred_at: datetime | None = None
```

and extend the optional-capability comment block on `AgentAdapter`:

```python
    #     def event(self, env: Mapping[str, str], payload: dict) -> HarnessEvent | None: ...
    #
    # `event()` is what makes `remem record event` harness-neutral. An
    # adapter that does not implement it records nothing, and the CLI says
    # so rather than guessing at a payload shape it does not understand.
    # Returning None is an ordinary answer - "this payload is not an event
    # worth recording" - and is how an adapter filters its harness's noise
    # without the service growing a per-harness branch. Same degradation
    # contract as env_settings()/settings_path(): a capability that raises
    # warns and continues; a broken third-party adapter must never be why
    # recording stops for everyone.
```

- [ ] **Step 4: Write the service**

Create `src/remem/services/record.py`:

```python
"""Recording: the per-project opt-in, and the single INSERT behind it.

The gate is the entire safety story of the events pipeline. Full payloads
mean this table will hold file contents, command output, and whatever a user
pasted into a prompt - so what decides whether any of that is ever written is
one boolean per project, checked here, in the service, where every frontend
gets it and no frontend can skip it.
"""
```

`record()` resolves the gate, then builds an `Event` with `new_id()` and the caller's harness name, then calls `store.put_event`. It returns `None` for a closed gate and for a project-less event, and it raises nothing on either - the hook path treats `None` as an ordinary outcome and the CLI turns it into a `REMEM_HOOK_DEBUG` line.

- [ ] **Step 5: Implement `event()` on the Claude Code adapter**

In `src/remem/agents/claude_code/adapter.py`:

```python
    #: Claude Code hook events this adapter records, mapped to event kinds.
    #: Anything not in this table returns None - an adapter that recorded
    #: every hook it was ever handed would fill `events` with lifecycle
    #: noise the extractor then has to read past.
    EVENT_KINDS = {
        "PostToolUse": EventKind.TOOL_CALL,
        "SessionEnd": EventKind.SESSION_END,
    }

    def event(self, env: Mapping[str, str], payload: dict) -> HarnessEvent | None:
        """Read one Claude Code hook payload as an event, or None.

        The payload is passed through WHOLE. Picking fields out here would
        make this adapter the thing that decides what the extractor is
        allowed to see, and the extractor is the half of this pipeline meant
        to be fixable and re-runnable without re-recording anything.
        """
        kind = self.EVENT_KINDS.get(payload.get("hook_event_name", ""))
        if kind is None:
            return None
        identity = self.identity(env, payload)
        if not identity.session_id:
            # Without a session id the event cannot be grouped for
            # extraction, so recording it would be storage with no reader.
            return None
        return HarnessEvent(
            kind=kind,
            session_id=identity.session_id,
            project=identity.project,
            tool=payload.get("tool_name"),
            payload=payload,
            occurred_at=datetime.now(timezone.utc),
        )
```

`occurred_at` is the moment we read it, not a field from the payload: Claude Code does not send one, and inventing a parse for a key that does not exist is how a NOT NULL column starts receiving nulls.

- [ ] **Step 6: Write the failing CLI test**

Create `tests/test_record_cli.py`. It uses `live_dsn` (the command opens its own connection through `open_session`), the pattern `tests/test_capture_cli.py` already establishes:

```python
def test_record_event_writes_one_row(live_dsn, monkeypatch, tmp_path):
    ...
    result = runner.invoke(app, ["record", "event"], input=json.dumps({
        "hook_event_name": "PostToolUse",
        "session_id": "s1",
        "cwd": str(repo),
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }))
    assert result.exit_code == 0
    ...one event, kind tool_call, tool Bash, payload holding tool_input...


def test_record_event_exits_zero_on_garbage_stdin(live_dsn):
    """Fail-soft: this runs as a hook on every tool call."""
    result = runner.invoke(app, ["record", "event"], input="not json")
    assert result.exit_code == 0
    assert result.stdout == ""


def test_record_event_explains_itself_under_hook_debug(live_dsn, monkeypatch):
    monkeypatch.setenv("REMEM_HOOK_DEBUG", "1")
    ...assert the reason names the project and `remem record enable`...


def test_an_adapter_whose_event_capability_raises_degrades(live_dsn, monkeypatch):
    """Same contract as env_settings()/settings_path(): warn, degrade, keep
    going. A broken third-party adapter must never be why recording stops
    for everyone - and on this path, "stops" would be silent."""
    monkeypatch.setattr(ClaudeCodeAdapter, "event", boom)
    result = runner.invoke(app, ["record", "event"], input=json.dumps({...}))
    assert result.exit_code == 0


def test_an_adapter_with_no_event_capability_says_so(live_dsn, monkeypatch):
    """Not an error and not a crash - "this agent cannot record" is an
    answer, and it is the one Cursor and opencode give until their adapters
    ship."""
```

- [ ] **Step 7: Add the CLI commands**

In `src/remem/cli.py`:

```python
record_app = typer.Typer(help="Record raw events from a harness.")
app.add_typer(record_app, name="record")
```

`record event` reads stdin, resolves the adapter by `--agent` (default `claude-code`) through `agents.registry`, probes `event()` with `getattr`, and hands the result to `services.record.record`. It exits 0 unconditionally and prints nothing on stdout: this is a hook entry point in everything but name, and it runs once per tool call. Every early return writes its reason through the same `_debug` helper the hooks use - lift that helper out of `agents/claude_code/hook.py` into `remem/hookio.py` so the CLI and the hooks share one definition rather than two that drift.

A malformed payload is the one loud case, per the spec's error table: `remem record event` invoked by hand with unreadable JSON prints to stderr and exits 1 **only when stdin is a TTY or `--strict` is passed**; from a hook it stays silent. Without that split, either the hook can break a session or a user debugging by hand gets no feedback at all.

`record enable` / `record disable` are the old `capture enable` / `capture disable`, moved and re-worded: the cost sentence now names `REMEM_EXTRACT_MODEL` (Task 10 renames the variable; until then it prints the old name and Task 10's step list updates it).

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/test_record_service.py tests/test_record_cli.py -v`
Expected: PASS.

- [ ] **Step 9: Run the whole suite and commit**

```bash
docker compose ps && uv run pytest
git add -A
git commit -m "Record events through remem record event, behind the opt-in"
```

---

### Task 5: `extract_jobs`

The spool for extraction, and the `covers_through` watermark that makes "already extracted" a fact rather than an inference.

This is a new table, **not a re-keyed `capture_jobs`**. The old key is `transcript_path not null`; the new one is `(owner_id, project, harness, session_id)`, and the two do not convert - a pending capture job names a transcript, while the new extractor's input is events, which do not exist for that session and never will. Any translation would be inventing rows.

**Files:**
- Create: `src/remem/backends/postgres/migrations/009_extract_jobs.sql`
- Create: `tests/test_extract_jobs_store.py`
- Modify: `src/remem/domain.py` (`JobStatus`, `ExtractJob`)
- Modify: `src/remem/store.py`, `src/remem/backends/postgres/store.py`

**Interfaces:**
- Consumes: `SessionRef` (Task 3).
- Produces:
  - `JobStatus(StrEnum)`: `PENDING`, `RUNNING`, `DONE`, `FAILED`.
  - `ExtractJob` dataclass: `id: UUID`, `owner_id: UUID`, `project: str`, `harness: str`, `session_id: str`, `covers_through: datetime | None = None`, `status: JobStatus = JobStatus.PENDING`, `attempts: int = 0`, `error: str | None = None`, `entries_written: int = 0`, `created_at: datetime | None = None`, `updated_at: datetime | None = None`.
  - `Store.sessions_awaiting_extraction(owner_id: UUID, idle_seconds: int, limit: int) -> list[SessionRef]`
  - `Store.claim_extract_job(owner_id: UUID, session: SessionRef) -> ExtractJob` - upsert on `(owner_id, project, harness, session_id)`, bumps `attempts`, sets `running`.
  - `Store.claim_extract_job_by_id(job_id: UUID, owner_id: UUID) -> ExtractJob | None`
  - `Store.finish_extract_job(job_id, owner_id, status: JobStatus, error: str | None, entries_written: int, covers_through: datetime | None) -> None`
  - `Store.get_extract_job(job_id, owner_id) -> ExtractJob | None`
  - `Store.extract_job_counts(owner_id) -> dict[str, int]`
  - `Store.recent_failed_extract_jobs(owner_id, limit: int = 5) -> list[ExtractJob]`
  - `Store.try_advisory_lock(name: str, owner_id: UUID) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/test_extract_jobs_store.py`. The tests that matter are the watermark ones - the rest is spool mechanics the capture tests already prove in their own shape:

```python
def test_a_session_with_recent_events_is_not_awaiting_extraction(store, owner):
    """The idle rule, from the store's side.

    A session that is still being worked in has events arriving; extracting
    it now means extracting half a session and then having to decide what to
    do with the other half.
    """
    put_event(store, owner, at=now_utc())
    assert store.sessions_awaiting_extraction(owner.id, idle_seconds=1200,
                                              limit=10) == []


def test_a_quiet_session_is_awaiting_extraction(store, owner):
    put_event(store, owner, at=now_utc() - timedelta(hours=2))
    [session] = store.sessions_awaiting_extraction(owner.id, 1200, 10)
    assert session.session_id == "s1"
    assert session.event_count == 1
    assert session.extract_from is None


def test_a_resumed_session_comes_back_with_a_watermark(store, owner):
    """The reason covers_through exists.

    "This session has a done job" is the obvious definition of extracted and
    it is wrong: a session that gets resumed records new events after that
    job, and under the obvious rule those events are born already-extracted -
    invisible to process, and eligible for prune having produced nothing.
    """
    first = now_utc() - timedelta(hours=3)
    put_event(store, owner, at=first)
    session = store.sessions_awaiting_extraction(owner.id, 1200, 10)[0]
    job = store.claim_extract_job(owner.id, session)
    store.finish_extract_job(job.id, owner.id, JobStatus.DONE, None, 2,
                             covers_through=first)

    assert store.sessions_awaiting_extraction(owner.id, 1200, 10) == []

    resumed_at = now_utc() - timedelta(hours=1)
    put_event(store, owner, at=resumed_at)

    [again] = store.sessions_awaiting_extraction(owner.id, 1200, 10)
    assert again.extract_from == first
    assert again.event_count == 1  # only the unextracted one


def test_claiming_twice_reuses_the_row_and_counts_attempts(store, owner):
    ...

def test_the_advisory_lock_is_per_command_and_owner(store, owner):
    """Two `events process` runs must not process the same session; a
    process run and an embed run must not block each other."""
    assert store.try_advisory_lock("events-process", owner.id) is True
    other = store.ensure_principal("someone-else")
    assert store.try_advisory_lock("events-process", other.id) is True
    assert store.try_advisory_lock("embed", owner.id) is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_extract_jobs_store.py -v`
Expected: FAIL - `ImportError: cannot import name 'JobStatus'`.

- [ ] **Step 3: Write the migration**

Create `src/remem/backends/postgres/migrations/009_extract_jobs.sql`:

```sql
-- The extraction spool. A NEW table, not a re-keyed capture_jobs: the old
-- key is a transcript path and the new one is a session, and the two do not
-- convert. A pending capture job names a transcript; the extractor's input
-- is events, which do not exist for that session and never will. Any
-- translation would be inventing rows. capture_jobs is retired untouched in
-- 010 instead.

-- A new enum rather than reusing capture_status, which 010 leaves in place
-- for the legacy table. Mutating an enum a live table still uses, in the
-- same migration that renames that table, is more moving parts than the
-- saving is worth.
create type job_status as enum ('pending','running','done','failed');

create table extract_jobs (
  id uuid primary key,
  owner_id uuid not null references principals(id),
  project text not null,
  harness text not null,
  session_id text not null,
  -- The newest occurred_at among the events this job actually read.
  -- "This session has a done job" would be the obvious definition of
  -- extracted, and it is wrong for a resumed session: events recorded after
  -- that job would be born already-extracted, invisible to process and
  -- eligible for prune having produced nothing. The watermark makes the
  -- question answerable per event rather than per session.
  covers_through timestamptz,
  status job_status not null default 'pending',
  attempts int not null default 0,
  error text,
  entries_written int not null default 0,
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  unique (owner_id, project, harness, session_id)
);

create index extract_jobs_pending_idx on extract_jobs (owner_id, created_at)
  where status = 'pending';
```

- [ ] **Step 4: Add the domain types and Protocol methods**

`JobStatus` replaces `CaptureStatus` in `domain.py`; `ExtractJob` sits beside it. Leave `CaptureJob` and `CaptureStatus` in place for now - `services/capture.py` still imports them and Task 6 is what deletes both.

- [ ] **Step 5: Implement the store methods**

The one with substance is `sessions_awaiting_extraction`. It groups events by `(project, harness, session_id)`, joins the newest done job's `covers_through`, keeps only groups holding events past that watermark, and drops any group whose newest event is inside the idle window:

```sql
            with watermarks as (
              select project, harness, session_id, max(covers_through) as mark
                from extract_jobs
               where owner_id = %(owner_id)s and status = 'done'
               group by project, harness, session_id
            )
            select e.project, e.harness, e.session_id,
                   count(*) as event_count,
                   max(e.occurred_at) as last_event_at,
                   w.mark as extract_from
              from events e
              left join watermarks w
                     on w.project = e.project
                    and w.harness = e.harness
                    and w.session_id = e.session_id
             where e.owner_id = %(owner_id)s
               and (w.mark is null or e.occurred_at > w.mark)
             group by e.project, e.harness, e.session_id, w.mark
            having max(e.occurred_at)
                     < clock_timestamp() - make_interval(secs => %(idle)s)
             order by max(e.occurred_at)
             limit %(limit)s
```

`event_count` counts only the unextracted events, which is what makes the count in `remem record status` mean "work outstanding" rather than "events that exist".

`claim_extract_job` is an upsert on the unique key with `attempts = extract_jobs.attempts + 1` and `status = 'running'`, so a retried session reuses its row and its attempt count rather than accumulating one row per attempt.

`try_advisory_lock` uses the two-integer form, keyed on a stable hash of the command name and the owner id, and takes a **session-level** lock:

```python
        # Session-level, not transaction-level: `events process` runs with
        # autocommit on, so a transaction-scoped lock would be released at
        # the first commit - which is the first job it finishes, exactly
        # when a second run must still be kept out. The lock dies with the
        # connection, which is the process ending, which is what we want.
```

- [ ] **Step 6: Run the tests, then the suite, then commit**

```bash
uv run pytest tests/test_extract_jobs_store.py -v
docker compose ps && uv run pytest
git add -A
git commit -m "Add the extraction spool, keyed on sessions"
```

---

### Task 6: Extraction - events in, entries and provenance out

The largest task, and it cannot be smaller: this is where the pipeline actually swaps over. Capture's transcript path stops being read, `services/capture.py` becomes `services/extraction.py`, `remem/distill/` becomes `remem/extract/`, `capture_jobs` is retired, and `remem capture drain` becomes `remem events process`. Splitting any of it out leaves the tree with two extractors, or with none.

What does **not** change is the extractor's contract, and it is worth naming because it is easy to lose in a rename: the model stays pinned, all output stays untrusted, `MAX_ENTRIES`/`MAX_TITLE`/`MAX_BODY` still cap it, and it is still filtered before it reaches the store. Only the input changes - a list of event rows rather than a transcript file.

**Files:**
- Create: `src/remem/services/extraction.py` (from `services/capture.py`)
- Create: `src/remem/extract/__init__.py`, `src/remem/extract/base.py`, `src/remem/extract/claude_cli.py` (from `distill/`)
- Create: `src/remem/backends/postgres/migrations/010_retire_capture_jobs.sql`
- Create: `tests/test_extraction_service.py`, `tests/test_events_process_cli.py`
- Delete: `src/remem/services/capture.py`, `src/remem/distill/`, `tests/test_capture_service.py`, `tests/test_capture_store.py`, `tests/test_capture_autodrain.py`
- Rename: `tests/test_distill_parsing.py` -> `tests/test_extract_parsing.py`, `tests/test_distill_claude_cli.py` -> `tests/test_extract_claude_cli.py`, `tests/test_distill_known_titles.py` -> `tests/test_extract_known_titles.py`, `tests/test_capture_origins.py` -> `tests/test_extract_origins.py`
- Modify: `src/remem/cli.py` (an `events` group; `capture drain` deleted), `src/remem/config.py` (`idle_minutes`), `src/remem/store.py` and `backends/postgres/store.py` (delete the `capture_jobs` methods)

**Interfaces:**
- Consumes: everything from Tasks 3 and 5.
- Produces:
  - `extract.base.ExtractedEntry` (was `CapturedEntry`), `extract.base.Extractor` Protocol with `extract(events: list[Event], project: str, known_titles: list[str] | None = None) -> list[ExtractedEntry]`, `extract.base.ExtractionFailed` (was `DistillationFailed`), `extract.base.CHILD_ENV_VAR` (renamed in Task 10, not here), `extract.base.parse_entries` unchanged.
  - `extract.claude_cli.ClaudeCliExtractor`, and `render_events(events: list[Event], limit: int = MAX_PROMPT_BYTES) -> str`.
  - `services.extraction.process(store, owner_id, extractor, idle_seconds, limit) -> ExtractReport`
  - `services.extraction.process_job(store, owner_id, job_id, extractor) -> ExtractReport`
  - `services.extraction.ExtractReport`: `claimed`, `succeeded`, `failed`, `entries_written`.
  - `services.extraction.ExtractJobNotFound`
  - CLI: `remem events process [--limit] [--job ID]`
  - `config.Config.idle_minutes: int` (default 20, `REMEM_IDLE_MINUTES`)

- [ ] **Step 1: Write the failing round-trip test**

Create `tests/test_extraction_service.py`. The centre of it is the spec's round-trip requirement - record events, process, assert entries **and** `entry_events` rows:

```python
class FakeExtractor:
    """Records what it was given, returns what it was told to.

    A fake rather than a stub because the interesting assertion is about the
    INPUT: the extractor must be handed events, and never a transcript path.
    """

    def __init__(self, entries):
        self.entries = entries
        self.seen: list[list[Event]] = []

    def extract(self, events, project, known_titles=None):
        self.seen.append(list(events))
        return list(self.entries)


def test_processing_a_quiet_session_writes_entries_and_provenance(store, owner):
    record.enable(store, owner.id, "remem")
    events = [record.record(store, owner.id, a_harness_event(
        occurred_at=now_utc() - timedelta(hours=2)), "claude-code")
        for _ in range(3)]
    extractor = FakeExtractor([ExtractedEntry(
        title="pgvector needs no index yet", body="...", kind=Kind.NOTE,
        tags=["postgres"])])

    report = extraction.process(store, owner.id, extractor,
                                idle_seconds=1200, limit=10)

    assert (report.claimed, report.succeeded, report.entries_written) == (1, 1, 1)
    [written] = store.search(Query(project="remem",
                                   origins=[Origin.EXTRACTED], limit=10),
                             owner.id)
    assert written.entry.origin is Origin.EXTRACTED
    assert written.entry.session_id == "s1"
    rows = store.provenance(written.entry.id, owner.id)
    assert {r[0] for r in rows} == {e.id for e in events}
    assert all(r[1:] == ("s1", "claude-code", True) for r in rows)


def test_the_extractor_is_handed_events_not_a_transcript(store, owner):
    ...assert extractor.seen == [the three events, oldest first]...


def test_a_second_run_extracts_nothing_new(store, owner):
    """Idempotence. `events process` is a cron command; running it twice
    must do the work once."""
    ...process twice, assert the second report is all zeros...


def test_a_session_still_being_worked_in_is_left_alone(store, owner):
    ...one event at now, process, assert claimed == 0...


def test_a_resumed_session_extracts_only_its_new_events(store, owner):
    ...process, record two more events, process again,
       assert the extractor saw only the two...


def test_an_extractor_that_raises_fails_the_job_and_records_why(store, owner):
    ...status failed, error holds both the reason and the raw output...


def test_the_attempt_cap_stops_a_job_and_process_job_overrides_it(store, owner):
    ...


def test_an_entry_the_project_already_holds_is_not_rewritten(store, owner):
    """Carried over from capture unchanged: only a prior EXTRACTED entry
    with the same title suppresses a write. A human-written entry with that
    title is not a duplicate to swallow silently."""
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_extraction_service.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'remem.services.extraction'`.

- [ ] **Step 3: Rename the package**

```bash
git mv src/remem/distill src/remem/extract
git mv tests/test_distill_parsing.py tests/test_extract_parsing.py
git mv tests/test_distill_claude_cli.py tests/test_extract_claude_cli.py
git mv tests/test_distill_known_titles.py tests/test_extract_known_titles.py
```

Then rename inside: `CapturedEntry` -> `ExtractedEntry`, `DistillationFailed` -> `ExtractionFailed`, `Distiller` -> `Extractor`, `distill()` -> `extract()`, `ClaudeCliDistiller` -> `ClaudeCliExtractor`. Update the imports in the renamed tests. `parse_entries` and its whole "first array that yields a valid entry" argument are untouched - that logic is about model output, not about input, and nothing in this task changes what a model returns.

- [ ] **Step 4: Teach the extractor to read events**

In `src/remem/extract/claude_cli.py`, `bound_transcript` becomes `render_events`, keeping the measured bound and the reason for it:

```python
# Measured against the real CLI on a long session's transcript:
#   40KB  -> returns in seconds, and the model follows the prompt
#   400KB -> ~5 minutes, AND the model ignores the prompt entirely,
#            continuing the conversation instead of extracting from it
#   5.9MB -> claude exits 1
# The second case is the dangerous one: it does not announce itself. Output
# comes back as prose, parse_entries raises, and the job looks like a bad
# prompt rather than an oversized input. The bound applies to the RENDERED
# events for the same reason it applied to the transcript - it is a property
# of the model's attention, not of where the text came from.
MAX_PROMPT_BYTES = 40_000

TRUNCATION_NOTE = (
    "[This is the TAIL of a longer session; earlier events were truncated.]\n"
)


def render_events(events: list[Event], limit: int = MAX_PROMPT_BYTES) -> str:
    """One line per event, oldest first, newest kept.

    Keeping the END for the same reason the transcript version did: a
    session's conclusions, decisions and corrections live at its end, and its
    opening is setup. The note matters as much as the truncation - without
    it the model reasons about a session that appears to begin mid-thought.

    The payload is rendered as compact JSON rather than summarised. What is
    worth keeping from a tool call is exactly the judgement this pipeline
    delegates to the model, and pre-digesting it here would make the
    extractor unable to see anything the renderer decided to drop - while
    the whole point of storing raw is that a better prompt can be re-run
    over the same events.
    """
```

The prompt itself changes only where it says "transcript": it now reads a list of events from one session. Everything about what to record and what not to - no credentials, nothing transient, `[]` is the common and correct answer - is unchanged, because none of it depended on the input format.

- [ ] **Step 5: Write the service**

`git mv src/remem/services/capture.py src/remem/services/extraction.py`, then rework:

- `enqueue()` is **deleted**. Nothing enqueues any more; `process` discovers sessions through `sessions_awaiting_extraction` and claims them. This is the idle trigger, and it is the reason Claude Code no longer needs a `SessionEnd` hook to make the pipeline work.
- `_run_job` reads `store.events_for_session(..., since=session.extract_from)` instead of `Path(job.transcript_path).read_text()`. An empty event list finishes the job `done` with zero entries rather than failing: a session whose only events were filtered out is a quiet session, not a broken one.
- On success it writes entries, then `store.link_entry_events(entry.id, events, owner_id)` for each, then finishes the job with `covers_through=max(e.occurred_at for e in events)`.
- The `MAX_ATTEMPTS` cap, `_failure_reason` (reason plus truncated raw output, truncated separately so a long response cannot clip the reason away), `_safe_finish`, `_known_titles` and `_already_captured` (renamed `_already_extracted`) carry over unchanged in substance.

Provenance is written **per entry against every event in the batch**, not against a guessed subset. The extractor does not tell us which event produced which entry, and inventing an attribution would put a false fact in an audit table. "This entry came out of this session's events" is what we actually know.

- [ ] **Step 6: Retire `capture_jobs`**

Create `src/remem/backends/postgres/migrations/010_retire_capture_jobs.sql`:

```sql
-- The old spool, renamed and left alone.
--
-- Not dropped: a pending capture job names a transcript a user may still
-- want distilled by hand, and deleting it silently is the one thing this
-- migration must not do. Not translated either - see 009 for why. So it
-- sits here, unread, with no code path referring to it, until the user
-- confirms they want nothing from it and a later migration drops it.
-- `remem record status` mentions it once if any row is still pending, so
-- the dead spool is visible rather than mysterious.
--
-- capture_status is deliberately left in place: it is this table's column
-- type, and mutating an enum a live table still uses buys nothing here.

alter table capture_jobs rename to capture_jobs_legacy;
alter index capture_jobs_pending_idx rename to capture_jobs_legacy_pending_idx;
```

Then delete every `capture_jobs` method from `store.py` and `backends/postgres/store.py`, and `CaptureJob`/`CaptureStatus` from `domain.py`. Add one method in their place: `Store.pending_legacy_capture_jobs(owner_id: UUID) -> int`, which Task 8 surfaces.

- [ ] **Step 7: Add `remem events process` and delete `remem capture drain`**

```python
events_app = typer.Typer(help="Extraction and retention for recorded events.")
app.add_typer(events_app, name="events")
```

`events process` keeps the two things `capture drain` got right and adds one:

- **Autocommit**, for the reason already documented on the drain: the run records its own progress and spends minutes inside `claude`, so one transaction for the batch would discard succeeded work on a later error and hold row locks across those minutes.
- **`--job ID`** retries one job past the attempt cap - the only way back for a job that gave up.
- **The advisory lock.** `store.try_advisory_lock("events-process", owner.id)`; if it returns False the command prints nothing and exits 0. A cron run that overlaps its predecessor has nothing to say, and a non-zero exit there would mail the user about a working system.

Its output is the inverse of a hook's: nothing on success beyond the one-line report, non-zero and explanatory on failure.

- [ ] **Step 8: Put `remem embed` behind the same lock**

The spec's cron-safety rule is every command in the pipeline, not just the new one. `remem embed` already claims batches idempotently, but two overlapping runs embed the same backlog twice and pay for it twice. One line in `cli.py`:

```python
        if not s.store.try_advisory_lock("embed", s.owner.id):
            # A previous run is still going. Silence and exit 0 - a cron
            # command that mails the user about a working system is a cron
            # command they will turn off.
            raise typer.Exit(0)
```

Add a test to `tests/test_embed_service.py` asserting a second run with the lock held does nothing and exits 0.

- [ ] **Step 9: Add `idle_minutes` to config**

In `config.py`, `DEFAULT_IDLE_MINUTES = 20` and `idle_minutes` resolved through the existing `positive_int` helper - which already falls back on a non-positive value, and here that matters: a zero window makes every session extractable the instant its first event lands, which is extraction racing a session that is still writing.

- [ ] **Step 10: Run everything**

Run: `docker compose ps && uv run pytest`
Expected: 0 skipped, and no test importing `remem.distill` or `remem.services.capture` remains. `grep -rn "distill\|services.capture\|capture_jobs\b" src tests` returns nothing outside `010_retire_capture_jobs.sql` and the legacy-count method.

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "Extract entries from events, on an idle trigger"
```

---

### Task 7: `remem events prune`

Deleting raw is the one irreversible thing in this pipeline, so the command is built to refuse.

- **No default window.** `--before` is required. A configured retention number quietly deleting history is exactly what the spec's retention section rules out, and a default here would be that number wearing a flag.
- **Extracted only.** An unextracted event is raw that has produced nothing; losing it is the outcome the whole design exists to prevent. `--force` overrides, loudly, for a stuck session.
- **Report the dangling.** Prune says how many `entry_events` rows it just left pointing at nothing. That number is the cost of the run, and this is the only moment a user can see it.

**Files:**
- Create: `src/remem/services/events.py`, `tests/test_events_prune.py`
- Modify: `src/remem/store.py`, `src/remem/backends/postgres/store.py`, `src/remem/cli.py`
- Modify: `tests/test_store_events.py` (remove the xfail marker Task 3 added)

**Interfaces:**
- Consumes: `Store.provenance`, the job tables from Task 5.
- Produces:
  - `services.events.parse_window(text: str) -> timedelta` - accepts `30d`, `12h`, `90m`; raises `BadWindow` otherwise.
  - `services.events.BadWindow`
  - `services.events.PruneRefused` - carries `unextracted: int`.
  - `services.events.PruneReport`: `deleted: int`, `dangling: int`, `kept_unextracted: int`.
  - `services.events.prune(store, owner_id, *, before: datetime, force: bool = False) -> PruneReport`
  - `Store.prune_events(owner_id, before: datetime, force: bool) -> tuple[int, int, int]` - `(deleted, dangling, kept_unextracted)`.
  - CLI: `remem events prune --before 30d [--force] [--json]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_events_prune.py`:

```python
def test_prune_without_a_window_is_refused_at_the_cli(live_dsn):
    """There is no default retention window, and this is where that is true.

    A default here would be a configured number quietly deleting history,
    which is the one thing the retention decision rules out. The refusal is
    the feature.
    """
    result = runner.invoke(app, ["events", "prune"])
    assert result.exit_code != 0
    assert "--before" in result.stdout + str(result.stderr)


def test_prune_refuses_unextracted_events(store, owner):
    ...one old, unextracted event...
    with pytest.raises(events.PruneRefused) as exc:
        events.prune(store, owner.id, before=now_utc())
    assert exc.value.unextracted == 1
    assert store.events_for_session(...) != []  # nothing deleted


def test_force_deletes_unextracted_events(store, owner):
    report = events.prune(store, owner.id, before=now_utc(), force=True)
    assert report.deleted == 1


def test_prune_leaves_entries_and_provenance_intact(store, owner):
    """The entry survives, its provenance row survives, and only the raw
    goes. Entries must be self-contained: nothing may read an event back at
    read time, or pruning would silently break retrieval."""
    ...extract, then prune...
    assert store.get_entry(entry.id, owner.id) is not None
    assert store.provenance(entry.id, owner.id) == [(event_id, "s1",
                                                     "claude-code", False)]


def test_prune_reports_what_it_left_dangling(store, owner):
    report = events.prune(store, owner.id, before=..., force=False)
    assert report.dangling == 3


def test_events_inside_the_window_are_kept(store, owner):
    ...

@pytest.mark.parametrize("text,expected", [
    ("30d", timedelta(days=30)), ("12h", timedelta(hours=12)),
    ("90m", timedelta(minutes=90)),
])
def test_window_parsing(text, expected):
    assert events.parse_window(text) == expected


@pytest.mark.parametrize("text", ["", "30", "d30", "-5d", "30 days", "0d"])
def test_a_window_that_does_not_parse_is_refused(text):
    """Including "30" with no unit. Guessing a unit for a bare number is how
    a user who meant 30 days deletes 30 minutes' worth - or everything."""
    with pytest.raises(events.BadWindow):
        events.parse_window(text)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_events_prune.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'remem.services.events'`.

- [ ] **Step 3: Implement the store side**

`prune_events` counts before it deletes, inside one statement each, and returns all three numbers. "Extracted" uses the same watermark rule as Task 5, so prune and process cannot disagree about what has been extracted:

```sql
            with watermarks as (
              select project, harness, session_id, max(covers_through) as mark
                from extract_jobs
               where owner_id = %(owner_id)s and status = 'done'
               group by project, harness, session_id
            ), candidates as (
              select e.id
                from events e
                left join watermarks w
                       on w.project = e.project and w.harness = e.harness
                      and w.session_id = e.session_id
               where e.owner_id = %(owner_id)s
                 and e.occurred_at < %(before)s
                 and (%(force)s or (w.mark is not null
                                    and e.occurred_at <= w.mark))
            )
            delete from events where id in (select id from candidates)
            returning id
```

The dangling count is `select count(*) from entry_events where event_id = any(deleted_ids)`, taken **before** the delete - after it, the rows still exist but the events they name do not, and counting then would mean re-deriving what we just did.

- [ ] **Step 4: Implement the service and the CLI**

`services/events.py` owns `parse_window` (no bare numbers, no zero, no negatives), the `--force` policy, and the refusal. `cli.py` formats:

```
deleted 412 events, kept 3 unextracted, left 27 provenance rows dangling
```

The dangling line prints even when the number is zero. A count that only appears when it is non-zero teaches the reader that its absence means nothing happened, and here absence would be indistinguishable from a prune that never reported.

- [ ] **Step 5: Un-xfail Task 3's provenance test**

Remove the `xfail` marker from `test_provenance_survives_the_events_it_names` in `tests/test_store_events.py` and run it for real.

- [ ] **Step 6: Run the suite and commit**

```bash
docker compose ps && uv run pytest
git add -A
git commit -m "Add remem events prune, which refuses more than it deletes"
```

---

### Task 8: `remem record status` and the forensic lookup

Fail-soft hooks make "recording nothing, silently, forever" the default failure mode. claude-mem shipped an opencode integration bound to event names opencode never emitted; nothing was recorded for months and `install` reported success throughout. remem has the same exposure. This task is half of the answer - silence made visible on demand. Task 9 is the other half.

**Files:**
- Create: `tests/test_record_status.py`
- Modify: `src/remem/services/events.py` (`status`, `forensics`), `src/remem/store.py`, `src/remem/backends/postgres/store.py`, `src/remem/cli.py`

**Interfaces:**
- Consumes: the job and event tables.
- Produces:
  - `Store.event_stats(owner_id) -> list[HarnessStats]` where `HarnessStats` is a `domain` dataclass: `harness: str`, `events_24h: int`, `last_event_at: datetime | None`, `sessions_awaiting: int`.
  - `services.events.status(store, owner_id, idle_seconds) -> StatusReport` bundling `harnesses`, `enabled_projects`, `job_counts`, `recent_failures`, `legacy_pending`, `extract_model`.
  - `services.events.forensics(store, owner_id, entry_id) -> list[ProvenanceRow]`.
  - CLI: `remem record status [--json]`, `remem events show ENTRY_ID`.

- [ ] **Step 1: Write the failing test**

```python
def test_status_reports_a_harness_that_has_recorded_nothing(store, owner):
    """The failure this whole command exists for.

    An adapter wired to hook names its harness never emits records nothing
    and reports success while doing it. The only way that becomes visible is
    a number the user can look at, so a harness with no events must appear
    in the report - as a zero - rather than being absent from it.
    """
    record.enable(store, owner.id, "remem")
    report = events.status(store, owner.id, idle_seconds=1200)
    assert report.enabled_projects == ["remem"]
    assert report.harnesses == []
    assert "no events" in events.render(report)


def test_status_counts_events_in_the_last_day_per_harness(store, owner):
    ...one event 2h old, one 40h old, assert events_24h == 1 and
       last_event_at is the newer...


def test_status_names_sessions_still_awaiting_extraction(store, owner):
    ...


def test_status_mentions_a_stranded_legacy_capture_job_once(live_dsn):
    """The dead spool must be visible rather than mysterious."""
    ...insert a pending row into capture_jobs_legacy...
    assert "capture_jobs_legacy" in result.stdout


def test_events_show_says_pruned_rather_than_not_found(store, owner):
    """"We recorded where this came from and then deleted the raw" and "we
    never recorded anything" are different answers, and a user who cannot
    tell them apart concludes provenance was never recorded at all."""
    ...prune, then show...
    assert "event pruned" in rendered
    assert "not found" not in rendered


def test_events_show_on_an_entry_with_no_provenance(store, owner):
    """A hand-written entry has no events and never will. That is an
    ordinary answer, not an error."""
```

- [ ] **Step 2: Run it, implement, run it again**

Run: `uv run pytest tests/test_record_status.py -v` - fails on the missing `status`. Implement `event_stats` (one grouped query over `events`, plus a count from `sessions_awaiting_extraction`), `status`, `forensics` and `render`, then re-run.

The rendering lives in `services/events.py` rather than `cli.py` for one specific reason worth writing in the code: `--json` and the human output must not be able to disagree about what "awaiting" means, and the surest way to guarantee that is one function producing the numbers and two thin formatters.

- [ ] **Step 3: Run the suite and commit**

```bash
docker compose ps && uv run pytest
git add -A
git commit -m "Make silence visible: remem record status, remem events show"
```

---

### Task 9: The Claude Code adapter, rewired and verified

The adapter stops queueing capture and starts recording events, and its install stops reporting success on faith.

The shape change is worth naming: with the idle trigger, **Claude Code no longer needs a SessionEnd hook for the pipeline to work**. It keeps one anyway, because a `session_end` event shortens the idle wait - but it is now a hint, never a requirement, which is what makes this adapter the same shape as the two that cannot provide one.

**Files:**
- Modify: `src/remem/agents/claude_code/adapter.py`, `src/remem/agents/claude_code/hook.py`, `src/remem/cli.py`
- Create: `tests/test_claude_code_events_install.py`, `tests/test_record_hook.py`
- Modify: `tests/test_claude_code_install.py`, `tests/test_capture_hook.py` (renamed to `tests/test_record_hook.py`), `tests/test_hook.py`

**Interfaces:**
- Consumes: `ClaudeCodeAdapter.event()` (Task 4), `services.record.record`, `services.extraction.process`.
- Produces:
  - `hook.record_event(stdin_text, env) -> None` and `hook.main_record_event() -> int`.
  - `ClaudeCodeAdapter.verify(env, home) -> InstallReport` - the live round-trip.
  - CLI: `remem hook record-event`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_install_registers_a_posttooluse_hook(tmp_path):
    ...settings.json holds "remem hook record-event" under PostToolUse...


def test_the_install_still_registers_session_start(tmp_path):
    """Context injection is untouched by this change."""


def test_the_session_end_hook_records_an_event_and_enqueues_nothing(...):
    """The idle rule replaced the end hook as the trigger. SessionEnd is now
    a hint that shortens the wait, and a harness without one loses nothing
    but time."""


def test_the_hook_exits_zero_when_the_database_is_unreachable(monkeypatch):
    """Fail-soft, on the path that now runs once per tool call."""
    ...REMEM_DSN pointing at a closed port, assert exit 0 and empty stdout...


def test_the_hook_refuses_to_recurse_inside_an_extraction_child(monkeypatch):
    """CHILD_ENV_VAR is checked by every hook. The extractor spawns
    `claude -p`, whose own hooks would otherwise record the extraction as
    events, which the next extraction would then read."""


def test_install_verification_round_trips_a_real_event(live_dsn):
    """An install that cannot demonstrate recording says so.

    claude-mem's opencode integration reported success for months while
    recording nothing. One live round-trip - record, read back, delete - is
    what makes that failure loud at the only moment the user is watching.
    """
    report = ClaudeCodeAdapter().verify(env=..., home=...)
    assert any("round-trip" in a for a in report.actions)
    assert report.warnings == []


def test_install_verification_reports_a_failure_rather_than_raising(...):
    ...unreachable database -> a warning naming what could not be done,
       exit code still 0, install still finishes...
```

- [ ] **Step 2: Register the new hook**

In `adapter.py`, add `RECORD_EVENT_COMMAND = "remem hook record-event"` and register it for `PostToolUse` with a short timeout - it runs once per tool call and does one INSERT, so it gets the 5-second budget `UserPromptSubmit` has, not the 10-second one `SessionStart` needs. Keep `SessionEnd` registered, pointed at the same command: `event()` maps both payloads.

`CAPTURE_NOTE` becomes `RECORD_NOTE` and re-words to the new command:

```python
RECORD_NOTE = (
    "Recording is OFF until you enable it per project: "
    "`remem record enable --project <name>`. Nothing is recorded from a "
    "project you did not choose, and that gate is the whole privacy story - "
    "events are stored in full, including command output and file contents."
)
```

The second sentence is new and belongs there. The user is deciding whether to opt in; the size of what they are opting into is the fact they need at that moment.

- [ ] **Step 3: Rewire the hooks**

In `hook.py`:
- Add `record_event(stdin_text, env)`, which parses stdin, gets a `HarnessEvent` from the adapter, and calls `services.record.record`. Same fail-soft contract as every other hook, and the same `_debug` explanations - including the one that names `remem record enable` when the gate is closed.
- `session_end` no longer enqueues capture. It is now just `record_event` for a payload whose `hook_event_name` is `SessionEnd`; delete the separate function and point `remem hook session-end` at the same entry point, keeping the command name so an already-installed settings.json does not break.
- `spawn_drain` becomes `spawn_process`, running `remem events process` instead of `remem capture drain`. The reason it exists is unchanged and still worth its comment: any session drains the backlog, so extraction is never stranded by the session that produced it having ended.

- [ ] **Step 4: Add install verification**

```python
    def verify(self, env, home) -> InstallReport:
        """Record an event, read it back, delete it.

        An install that reports success without demonstrating anything is
        how claude-mem's opencode integration recorded nothing for months.
        The round-trip is deliberately end-to-end - it goes through the same
        `remem record event` the hook will call, not through a store handle
        the hook does not have - because what is being tested is the wiring,
        and every part of the wiring that this skips is a part that can be
        broken while the check passes.
        """
```

It writes into a reserved project name (`__remem_verify__`) with recording forced on for that project only, then deletes the event and the setting. A failure is a `warning` on the report, never an exception: install must finish and say what it could not prove.

Call it from `install()` as its last step, and add `remem verify --agent claude-code` so a user can re-run it later without reinstalling.

- [ ] **Step 5: Run everything and commit**

```bash
docker compose ps && uv run pytest
git add -A
git commit -m "Record events from Claude Code, and prove the install works"
```

---

### Task 10: Names outside the database, and the docs

The last of the spec's rename step, plus the documentation that makes the rest of it findable. This lands last because every earlier task would otherwise have to know which half of the rename it was standing in.

**`CHILD_ENV_VAR` is renamed on every side in the same commit or not at all.** It is set on the spawned `claude -p` and checked by all three hooks; renaming one side leaves the extractor's own child recording events, which the next extraction reads, without bound. CLAUDE.md already carries that warning - this is the commit it was written for.

**Files:**
- Modify: `src/remem/config.py`, `src/remem/extract/base.py`, `src/remem/extract/claude_cli.py`, `src/remem/agents/claude_code/hook.py`, `src/remem/agents/claude_code/env_vars.py`, `src/remem/cli.py`, `src/remem/services/settings.py`
- Modify: `README.md`, `CLAUDE.md`
- Create: `tests/test_extract_names.py`
- Modify: `tests/test_env_vars.py`, `tests/test_config.py`, `tests/test_settings_values.py`

**Interfaces:**
- Consumes: everything.
- Produces: `config.DEFAULT_EXTRACT_MODEL`, `Config.extract_model`, `CHILD_ENV_VAR = "REMEM_EXTRACT_CHILD"`, and the hidden `capture` alias group.

- [ ] **Step 1: Write the failing test**

Create `tests/test_extract_names.py`:

```python
def test_the_old_model_variable_still_works_and_warns(capsys):
    """One release of grace, and a warning that names the replacement.

    A user's shell profile or settings.json holds REMEM_CAPTURE_MODEL today.
    Reading it silently would leave them believing they had pinned a model
    they had not; ignoring it silently would switch their model without
    telling them.
    """
    config = load(env={"REMEM_CAPTURE_MODEL": "opus"})
    assert config.extract_model == "opus"
    assert "REMEM_EXTRACT_MODEL" in capsys.readouterr().err


def test_the_new_variable_wins_when_both_are_set(capsys):
    config = load(env={"REMEM_CAPTURE_MODEL": "haiku",
                       "REMEM_EXTRACT_MODEL": "opus"})
    assert config.extract_model == "opus"


def test_the_child_variable_is_named_consistently_everywhere():
    """One string, checked by three hooks and set by the extractor. A
    rename that reaches only some of them lets an extraction's own child
    record events, which the next extraction reads, without bound."""
    from remem.extract.base import CHILD_ENV_VAR
    assert CHILD_ENV_VAR == "REMEM_EXTRACT_CHILD"
    source = (Path("src") / "remem").rglob("*.py")
    for path in source:
        text = path.read_text()
        assert "REMEM_CAPTURE_CHILD" not in text, path


def test_the_capture_commands_still_run_and_warn():
    result = runner.invoke(app, ["capture", "enable", "--project", "x"])
    assert result.exit_code == 0
    assert "remem record enable" in result.stdout


def test_no_credential_or_endpoint_variable_became_settable():
    """Standing rule, re-asserted because this task edits the table."""
```

- [ ] **Step 2: Rename the model variable**

In `config.py`, `DEFAULT_CAPTURE_MODEL` becomes `DEFAULT_EXTRACT_MODEL` and `capture_model` becomes `extract_model`. The fallback:

```python
    # REMEM_CAPTURE_MODEL is read for one release, and warns. The two silent
    # options are both wrong: reading it quietly leaves a user believing
    # they pinned a model when the name they set no longer exists, and
    # ignoring it quietly switches their model without telling them.
    # stderr, not stdout - a hook's stdout is the context block.
```

The measured reasoning on that default - Haiku returning 1 of 3 usable entries where Sonnet and Opus each returned 2 of 2, valid JSON every time, judgement the thing it got wrong - moves across with the constant. It is the whole argument for the pin.

- [ ] **Step 3: Rename `CHILD_ENV_VAR`, on every side**

`src/remem/extract/base.py` defines it; `src/remem/extract/claude_cli.py` sets it on the child's environment; `hook.py` checks it in three places. Change all five in this step, then `grep -rn REMEM_CAPTURE_CHILD src tests` must return nothing.

- [ ] **Step 4: Add the hidden aliases**

`remem capture enable|disable|status|drain` stay registered, hidden from `--help`, each printing one line to stderr naming its replacement and then delegating. They are muscle memory and they are in people's shell history; a command that has moved should say where, once, rather than failing with a usage error that does not name the new spelling.

- [ ] **Step 5: Update the settable env table**

In `agents/claude_code/env_vars.py` and `services/settings.py`, replace `REMEM_CAPTURE_MODEL` with `REMEM_EXTRACT_MODEL` and add `REMEM_IDLE_MINUTES` (INT, minimum 1). No credential and no endpoint variable becomes settable - their absence from the table is the enforcement, and `tests/test_env_vars.py` asserts it.

- [ ] **Step 6: Update the docs**

`README.md` gains the pipeline as the spec's four-line flow diagram plus the opt-in, and loses every mention of capture and transcripts. `CLAUDE.md`'s "### Capture" section becomes "### Events and extraction" and must state, at minimum:

- Recording is opt-in per project, checked in `services/record.py`, and that gate is the entire safety story.
- Events are stored in full and kept indefinitely; nothing prunes on a schedule.
- Extraction is triggered by idleness, not by a session-end hook - two of three harnesses do not have one.
- `entry_events.event_id` has no foreign key, deliberately, and no read path may dereference it.
- `covers_through` is what makes "already extracted" answerable per event.
- Entries with `origin='extracted'` are excluded from context blocks (`kb.resolve`) but appear in search; `search.DEFAULT_ORIGINS` must gain any future origin or that origin silently vanishes.
- `CHILD_ENV_VAR` is renamed on every side or not at all.

- [ ] **Step 7: Reinstall and smoke-test for real**

```bash
uv tool install --editable .
remem db status              # 010 applied, nothing pending
remem record enable --project remem
echo '{"hook_event_name":"PostToolUse","session_id":"smoke","cwd":"'$PWD'","tool_name":"Bash"}' | remem record event
remem record status          # one event, one session awaiting
remem events process         # nothing yet - the session is not idle
```

The hooks call the `remem` on PATH, so a change that only works in the project venv produces a config that silently does nothing. This step is what catches that.

- [ ] **Step 8: Run the suite and commit**

```bash
docker compose ps && uv run pytest
git add -A
git commit -m "Rename the last of capture, and document the pipeline"
```

---

## Verification

Before calling this plan done, all of it, in order:

1. `docker compose ps` - healthy, port 5433.
2. `uv run pytest` - **0 skipped**, and the passed count is above 543.
3. `uv run pytest -m slow` - the packaging test sees all four new migrations in the wheel.
4. `grep -rn "capture\|distill\|memory\b" src/ --include=*.py` - the only hits are `capture_jobs_legacy` and the hidden aliases.
5. `uv tool install --editable . && remem db up && remem record status` on a database migrated from before this branch, not a fresh one. The migration path is the part no unit test fully exercises: `git stash`, `remem db up` on the old code, restore, migrate forward, and check that an existing smart collection still resolves.

## Follow-ups deliberately not in this plan

- **Cursor and opencode adapters.** The spec proves the design supports them; they get their own plan, and the contract test asserting each subscribes only to events its harness actually emits belongs there.
- **Dropping `capture_jobs_legacy`.** A later migration, after the user confirms they want nothing from it.
- **The document corpus import.** The spec's own note: it is what makes the `entry_vectors` index question urgent, and it is an evaluation exercise rather than pipeline work.
- **A fourth `event_kind`.** Deferred until something writes one.
- **Cross-session extraction.** Might find patterns one session cannot, at a cost in prompt size and attribution clarity. Not in this design.
