# Semantic Recall Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make remem find entries by meaning, by adding a semantic tier between the existing exact and trigram tiers of search.

**Architecture:** Entries gain vectors in a separate `entry_vectors` table - derived, disposable, never a column on `entries`. An `Embedder` Protocol mirrors `store.py`'s portability seam, with one local ONNX implementation shipped as an optional dependency. `services/search.find()` grows from two tiers to three, still never blending: exact full-text, then semantic, then trigram, each running only when the previous returned nothing. `Hit.fuzzy: bool` becomes `Hit.match: Match`, because there are now three ways to have matched and every frontend must be able to say which.

**Tech Stack:** Python 3.14, Postgres 18 with pgvector (already the image in `compose.yaml`), psycopg 3 with hand-written SQL, `fastembed` (ONNX runtime, no PyTorch) as an optional extra, Typer, FastMCP, pytest.

**Spec:** `docs/superpowers/specs/2026-08-28-events-and-recall-design.md`

This plan implements one of the spec's three subsystems. The events pipeline and the Cursor/opencode adapters get their own plans and are **out of scope here**. Nothing in this plan touches `events`, `entry_events`, `extract_jobs`, or any rename of `capture`.

## Global Constraints

- **Python 3.14.** `from __future__ import annotations` at the top of every module; `StrEnum` for enums; `uuid7` from stdlib via `domain.new_id()`.
- **Frontends parse and format; they never decide.** Every policy branch - tier ordering, thresholds, limit clamping - lives in `services/`. A branch in `cli.py` that is not pure formatting is a bug in this plan's execution.
- **`store.py` is the portability seam.** New database operations are added to the `Store` Protocol first, then to `backends/postgres/store.py`. No SQL outside `backends/`.
- **`session.open_session()` is the only way to reach the database.** Nothing outside `session.py` and `backends/` imports psycopg.
- **Migrations are numbered `.sql` files, applied in filename order, and never edited once applied.** Add a new numbered file.
- **Timestamps use `clock_timestamp()`, never `now()`.** Tests run inside one rolled-back transaction, where `now()` gives every row an identical `created_at` and makes `order by created_at desc` non-deterministic.
- **Comments explain *why*, at length, especially where a decision looks arbitrary.** A subtle invariant with no comment reads as an accident to the next reader. Match the density of the surrounding code, which is high.
- **In prose, docs, comments and CLI output: spaced hyphens ` - `, never em dashes.**
- **A green pytest run means nothing unless the skip count is zero.** DB-marked tests `pytest.skip` when Postgres is unreachable. Before trusting any run: `docker compose ps` shows healthy on port 5433, and the summary line reads `0 skipped`. Baseline before this plan is **506 passed, 0 skipped**.
- **`uv tool install --editable .`** must be re-run after changing `pyproject.toml`, or the `remem` on PATH will not have the new optional dependency wiring.

## Decisions this plan settles

Two things the spec deferred to planning, ruled here so no task has to stop and think:

**1. `entry_vectors` gets no ANN index, and that is deliberate.** pgvector will not build an index on a `vector` column of unspecified dimension, which is what forces the spec's choice between a partial index per model and a table per dimension. Both are premature. At this store's size - tens to low thousands of entries - exact nearest-neighbour by sequential scan is milliseconds, and an HNSW index would be slower to build than the scan it replaces. The spec's own note says the index question becomes urgent at hundreds of thousands of vectors, which is the document-corpus evaluation, and by then the model and its dimension are known facts rather than guesses.

So: `vector` column, no dimension, no index, exact search. **The trigger to revisit is the corpus import, or `select count(*) from entry_vectors` passing roughly 50,000** - the point where a scan stops being free. Task 3 records this in the migration file itself, where the next person to wonder will actually be standing.

**2. The migration is `006_vectors.sql`.** The spec calls its migration `006_events.sql`, written when the events pipeline was expected to land first. Since semantic recall ships first, it takes `006` and the events plan takes `007`. Migration numbers record the order things were built, not the order the spec discusses them. **Task 3's final step updates the spec** so the two documents do not disagree.

---

## File Structure

**Created:**
- `src/remem/backends/postgres/migrations/006_vectors.sql` - the `entry_vectors` table, the `vector` extension, and the recorded reasoning about the absent index.
- `src/remem/embed.py` - the `Embedder` Protocol and the local ONNX implementation. One file, because the Protocol and its only implementation are read together and change together; this mirrors how `store.py` and `backends/postgres/store.py` split only once a second backend is imaginable.
- `src/remem/services/embed.py` - the embed-backlog policy: batching, which entries need work, what "current model" means.
- `tests/test_embed_protocol.py`, `tests/test_embed_service.py`, `tests/test_semantic_search.py`, `tests/test_vectors_schema.py`, `tests/test_match_marker.py`.

**Modified:**
- `src/remem/domain.py` - `Match` enum; `Hit.fuzzy` becomes `Hit.match`.
- `src/remem/store.py` - three new Protocol methods.
- `src/remem/backends/postgres/store.py` - `_entry_filters()` helper extracted, then `put_vectors`, `entries_missing_vectors`, `semantic_search`.
- `src/remem/services/search.py` - two tiers become three.
- `src/remem/config.py` - `embed_model` and `semantic_threshold`.
- `src/remem/cli.py` - `remem embed`; the search marker.
- `src/remem/mcp_server.py` - the search marker.
- `src/remem/services/settings.py` - the two new config keys in the settable table.
- `pyproject.toml` - the `embed` optional dependency group.

---

### Task 1: `Hit.match` replaces `Hit.fuzzy`

A boolean cannot express three outcomes. This lands first and separately because it touches every frontend, and doing it alongside the semantic tier would mix a mechanical rename into a behavioural change - the two would then have to be reviewed as one thing.

There is deliberately no `fuzzy` compatibility property. A frontend that still reads `.fuzzy` should fail loudly at import or attribute access, not silently report every semantic hit as exact.

**Files:**
- Modify: `src/remem/domain.py:139-152` (the `Hit` dataclass)
- Modify: `src/remem/backends/postgres/store.py:285-290` (search result construction), `:340-350` (fuzzy result construction)
- Modify: `src/remem/cli.py:250-267`
- Modify: `src/remem/mcp_server.py:130-141`
- Test: `tests/test_match_marker.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `remem.domain.Match` - a `StrEnum` with members `EXACT = "exact"`, `SEMANTIC = "semantic"`, `FUZZY = "fuzzy"`. `Hit.match: Match = Match.EXACT`. Tasks 5 and 7 construct `Hit(..., match=Match.SEMANTIC)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_match_marker.py`:

```python
"""Every frontend must be able to say HOW a hit matched.

A boolean could say "not exact". With three tiers the caller needs to know
which one answered: a semantic hit is a real conceptual match and worth
citing with care, while a trigram hit is a spelling accident that happened
to score. Collapsing them back into one "approximate" flag would throw away
the distinction the third tier exists to create.
"""

from remem.domain import Hit, Match


def _hit(**kw):
    from remem.domain import Entry, Kind, new_id
    entry = Entry(id=new_id(), kind=Kind.MEMORY, title="t", body="b",
                  owner_id=new_id())
    return Hit(entry=entry, rank=1.0, snippet="s", **kw)


def test_match_defaults_to_exact():
    assert _hit().match is Match.EXACT


def test_match_values_are_the_three_tiers():
    assert [m.value for m in Match] == ["exact", "semantic", "fuzzy"]


def test_match_is_a_string_enum():
    # Frontends serialise this straight into JSON, so it must render as the
    # bare word and not "Match.SEMANTIC".
    assert f"{Match.SEMANTIC}" == "semantic"


def test_hit_no_longer_has_a_fuzzy_boolean():
    # Deliberately no compatibility shim: a frontend still reading .fuzzy
    # would report every semantic hit as exact, silently.
    assert not hasattr(_hit(), "fuzzy")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_match_marker.py -v`
Expected: FAIL with `ImportError: cannot import name 'Match' from 'remem.domain'`

- [ ] **Step 3: Add `Match` and change `Hit`**

In `src/remem/domain.py`, add after the `Origin` enum:

```python
class Match(StrEnum):
    """How a hit matched, and therefore how much to trust it.

    Search runs three tiers and never blends them, so exactly one of these
    describes every hit in a result set. One field rather than an
    accumulating set of booleans: `fuzzy` alone could not distinguish a
    semantic match from a trigram one, and those deserve different trust.
    """

    EXACT = "exact"
    SEMANTIC = "semantic"
    FUZZY = "fuzzy"
```

Replace the `Hit` dataclass:

```python
@dataclass(slots=True)
class Hit:
    """A search result.

    `match` says which tier answered. Callers must be able to tell the
    difference: an agent handed an approximate match with no marker would
    cite it as certain.
    """

    entry: Entry
    rank: float
    snippet: str
    match: Match = Match.EXACT
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_match_marker.py -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Update the store's two result constructors**

In `src/remem/backends/postgres/store.py`, add `Match` to the `remem.domain` import list. In `search()`, the return becomes:

```python
        return [
            Hit(entry=_row_to_entry(r), rank=float(r["rank"]),
                snippet=r["snippet"], match=Match.EXACT)
            for r in rows
        ]
```

In `fuzzy_search()`, the equivalent return becomes `match=Match.FUZZY`. Both were previously relying on the `fuzzy` default; state it explicitly now, because with three values a default is no longer obviously right.

- [ ] **Step 6: Update the CLI**

In `src/remem/cli.py`, import `Match` from `remem.domain`. In `search()`, replace the JSON field and the two display branches:

```python
    if as_json:
        payload = []
        for h in hits:
            data = _entry_dict(h.entry, h.snippet)
            data["match"] = str(h.match)
            payload.append(data)
        typer.echo(json.dumps(payload, indent=2))
        return
    if not hits:
        typer.echo("No matches.")
        return
    # Say it once, up front: the caller should know how these were found
    # before reading any of them. Tiers never blend, so hits[0] speaks for
    # the whole result set.
    if hits[0].match is Match.SEMANTIC:
        typer.echo(f"No exact matches for {query!r}. Showing entries with "
                   f"related meaning:\n")
    elif hits[0].match is Match.FUZZY:
        typer.echo(f"No exact or related matches for {query!r}. Showing "
                   f"similar spellings:\n")
    for h in hits:
        marker = {Match.EXACT: "", Match.SEMANTIC: "~ ", Match.FUZZY: "? "}[h.match]
        typer.echo(f"{marker}{h.entry.id}  [{h.entry.kind}] {h.entry.title}")
        typer.echo(f"    {h.snippet}")
```

Update the command docstring:

```python
    """Search stored knowledge.

    Three tiers, tried in order and never blended: exact full-text, then
    entries with related meaning (marked ~), then typo-tolerant matching
    (marked ?). --json reports which as "match".
    --handoff also searches session handoffs, which are excluded by default.
    """
```

- [ ] **Step 7: Update the MCP server**

In `src/remem/mcp_server.py`, replace `"fuzzy": h.fuzzy,` with `"match": str(h.match),` and rewrite that paragraph of the tool docstring - it is the agent-facing contract and is the only thing telling a model how much to trust a result:

```python
    Every result carries "match", saying how it was found:
      "exact"    - the words are in the entry. Trust it.
      "semantic" - related in meaning, not in wording. Usually what you
                   meant, but read the entry in full via get_entry before
                   citing it.
      "fuzzy"    - a spelling-similarity guess made because nothing else
                   matched. Verify before relying on it at all.
    Tiers never mix: every result in one response has the same "match".
```

- [ ] **Step 8: Run the full suite**

Run: `uv run pytest`
Expected: PASS, 0 skipped. Existing tests asserting `hit.fuzzy` or a `"fuzzy"` JSON key will fail - update them to `match`, since the field genuinely changed. Check `tests/test_fuzzy_search.py`, `tests/test_services_search.py`, `tests/test_cli.py`, `tests/test_mcp_server.py`.

If the skip count is not zero, run `docker compose up -d`, confirm `docker compose ps` reports healthy, and run again. Do not proceed on a skipped suite.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "Replace Hit.fuzzy with Hit.match, which can say semantic"
```

---

### Task 2: Extract the duplicated entry filter builder

`search()` and `fuzzy_search()` contain the same twenty-line block building `where` clauses and params from a `Query`. Task 5 adds a third tier that needs the identical block. Copying it a third time is how the three drift apart, and a filter that silently stops applying to one tier is exactly the class of bug that returns another owner's rows.

Pure refactor: no behaviour changes, no new tests beyond proving the existing ones still pass.

**Files:**
- Modify: `src/remem/backends/postgres/store.py:228-260` and `:305-330`

**Interfaces:**
- Consumes: nothing.
- Produces: module-level `_entry_filters(query: Query, owner_id: UUID) -> tuple[list[str], dict]` in `backends/postgres/store.py`, returning `(where_clauses, params)`. Task 5 calls it.

- [ ] **Step 1: Confirm the current behaviour is covered before touching it**

Run: `uv run pytest tests/test_store_search.py tests/test_fuzzy_search.py -v`
Expected: PASS, 0 skipped. These are the tests that must still pass unchanged afterwards - a refactor with no test to catch it is a rewrite.

- [ ] **Step 2: Add the helper**

In `src/remem/backends/postgres/store.py`, after `entry_columns()`:

```python
def _entry_filters(query: Query, owner_id: UUID) -> tuple[list[str], dict]:
    """The filters every entry read applies, built once for all search tiers.

    Extracted because there are now three tiers running the same predicates
    against the same table. Duplicated, they drift: a filter accidentally
    dropped from one tier is invisible until that tier happens to answer, and
    the owner check is among them. One builder means one place to be wrong.

    Returns clauses joined by the caller with " and ", plus the params they
    reference. Text matching is NOT included - that is what differs between
    tiers and is the caller's business.
    """
    params: dict = {"owner_id": owner_id, "limit": query.limit}
    where = ["e.owner_id = %(owner_id)s"]

    if not query.include_superseded:
        where.append("e.superseded_by is null")
    if query.kinds:
        where.append("e.kind = any(%(kinds)s::entry_kind[])")
        params["kinds"] = [str(k) for k in query.kinds]
    if query.project is not None:
        where.append("e.project = %(project)s")
        params["project"] = query.project
    if query.tags:
        where.append("e.tags && %(tags)s")
        params["tags"] = list(query.tags)
    if query.origins:
        where.append("e.origin = any(%(origins)s::entry_origin[])")
        params["origins"] = [str(o) for o in query.origins]
    if query.since is not None:
        where.append("e.created_at >= %(since)s")
        params["since"] = query.since

    return where, params
```

- [ ] **Step 3: Use it in both existing tiers**

In `search()`, delete the block from `params: dict = {...}` through the `since` branch and replace with:

```python
        text = (query.text or "").strip()
        where, params = _entry_filters(query, owner_id)
```

In `fuzzy_search()`, delete the equivalent block and replace with:

```python
        where, params = _entry_filters(query, owner_id)
        params["text"] = text
        params["threshold"] = threshold
```

Leave everything below untouched in both.

- [ ] **Step 4: Run the same tests and confirm nothing moved**

Run: `uv run pytest tests/test_store_search.py tests/test_fuzzy_search.py tests/test_services_search.py -v`
Expected: PASS, 0 skipped, same test count as Step 1.

- [ ] **Step 5: Commit**

```bash
git add src/remem/backends/postgres/store.py
git commit -m "Build search filters in one place, before a third tier copies them"
```

---

### Task 3: Migration 006 - `entry_vectors`

**Files:**
- Create: `src/remem/backends/postgres/migrations/006_vectors.sql`
- Test: `tests/test_vectors_schema.py`
- Modify: `docs/superpowers/specs/2026-08-28-events-and-recall-design.md` (the migration filename)

**Interfaces:**
- Consumes: nothing.
- Produces: table `entry_vectors (entry_id uuid, model text, dim int, vector vector, created_at timestamptz)`, primary key `(entry_id, model)`, `on delete cascade` from `entries`. Tasks 5 and 6 read and write it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_vectors_schema.py`:

```python
"""entry_vectors is derived data, and the schema has to keep it that way.

Losing this table must cost a re-run and nothing else. That is what lets
the embedding model change without a migration: insert new rows, delete old
ones, never rewrite `entries`.
"""

import pytest

from remem.backends.postgres.migrate import migrate

pytestmark = pytest.mark.db


@pytest.fixture
def migrated(conn):
    migrate(conn)
    return conn


def test_vector_extension_is_installed(migrated):
    row = migrated.execute(
        "select 1 from pg_extension where extname = 'vector'"
    ).fetchone()
    assert row is not None


def test_primary_key_is_entry_and_model(migrated):
    row = migrated.execute("""
        select string_agg(a.attname, ',' order by a.attname)
        from pg_index i
        join pg_attribute a on a.attrelid = i.indrelid
                           and a.attnum = any(i.indkey)
        where i.indrelid = 'entry_vectors'::regclass and i.indisprimary
    """).fetchone()
    # One row per (entry, model) - which is what lets two models coexist
    # while a re-embed runs.
    assert row[0] == "entry_id,model"


def test_deleting_an_entry_deletes_its_vectors(migrated):
    from remem.backends.postgres.store import PostgresStore
    from remem.domain import Entry, Kind, new_id

    store = PostgresStore(migrated)
    owner = store.ensure_principal("vec-cascade")
    entry = store.put_entry(Entry(id=new_id(), kind=Kind.MEMORY, title="t",
                                  body="b", owner_id=owner.id))
    migrated.execute(
        "insert into entry_vectors (entry_id, model, dim, vector) "
        "values (%s, 'm', 2, '[0.1,0.2]')", (entry.id,)
    )
    migrated.execute("delete from entries where id = %s", (entry.id,))
    left = migrated.execute(
        "select count(*) from entry_vectors where entry_id = %s", (entry.id,)
    ).fetchone()[0]
    assert left == 0


def test_vector_column_has_no_declared_dimension(migrated):
    # Deliberate: see the migration's comment. A declared dimension would
    # commit the schema to one embedding model.
    row = migrated.execute("""
        select format_type(a.atttypid, a.atttypmod)
        from pg_attribute a
        where a.attrelid = 'entry_vectors'::regclass and a.attname = 'vector'
    """).fetchone()
    assert row[0] == "vector"


def test_there_is_no_ann_index_yet(migrated):
    # Asserted, not assumed. If someone adds an HNSW index they must come
    # here and say why the trigger in 006_vectors.sql was reached.
    rows = migrated.execute(
        "select indexname from pg_indexes where tablename = 'entry_vectors'"
    ).fetchall()
    assert [r[0] for r in rows] == ["entry_vectors_pkey"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_vectors_schema.py -v`
Expected: FAIL with `psycopg.errors.UndefinedTable: relation "entry_vectors" does not exist`

If instead every test SKIPS, Postgres is not running. `docker compose up -d`, wait for healthy, re-run. A skipped test has proved nothing.

- [ ] **Step 3: Write the migration**

Create `src/remem/backends/postgres/migrations/006_vectors.sql`:

```sql
-- Semantic recall: one vector per (entry, embedding model).
--
-- A separate table, not a column on entries. Vectors are DERIVED data, and
-- this codebase's rule is that derived data lives apart from its source, is
-- recomputable, and never overwrites it. Changing embedding model is then
-- inserting rows and deleting old ones - never a migration, never a rewrite
-- of the source row - and two models can coexist while a re-embed runs.
-- Losing this table costs a re-run and no data.

create extension if not exists vector;

create table entry_vectors (
  entry_id   uuid not null references entries(id) on delete cascade,
  model      text not null,
  dim        int  not null,
  vector     vector not null,
  created_at timestamptz not null default clock_timestamp(),
  primary key (entry_id, model)
);

-- `dim` is stored even though it is derivable from the vector, because it is
-- what makes a mismatch loud. A query embedded by a different model than the
-- rows it is compared against produces a pgvector error on dimension, which
-- is right, but the recorded dim lets the service say WHICH model disagreed
-- rather than surfacing the raw operator error.

-- NO APPROXIMATE-NEAREST-NEIGHBOUR INDEX, DELIBERATELY.
--
-- pgvector cannot index a `vector` column of unspecified dimension, and
-- declaring one here would commit the schema to a single embedding model -
-- the exact coupling this table exists to avoid. The alternatives are a
-- partial index per model (with a cast to a fixed dimension) or a table per
-- dimension. Both were considered and both are premature: at this store's
-- size, exact nearest-neighbour by sequential scan takes milliseconds, and
-- building an HNSW index would cost more than the scans it replaces.
--
-- REVISIT WHEN: entry_vectors passes roughly 50k rows, or the document
-- corpus is imported - whichever comes first. At that point the model in use
-- is a known fact rather than a guess, which is precisely what makes the
-- choice between the two options decidable. Until then, exact search is not
-- a compromise; it is the more accurate of the two, and free.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_vectors_schema.py -v`
Expected: PASS, 5 tests, 0 skipped.

- [ ] **Step 5: Confirm the migration also applies to a real database**

Run: `remem db up && remem db status`
Expected: `006_vectors` listed as applied, nothing pending.

This catches what the test cannot: the test database is created fresh, while the real one applies `006` on top of five existing migrations and real rows.

- [ ] **Step 6: Correct the spec's migration filename**

The spec says the events migration is `006_events.sql`. Semantic recall took `006`, so events becomes `007`. In `docs/superpowers/specs/2026-08-28-events-and-recall-design.md`, in the "Migration from capture" section, change `One migration, \`006_events.sql\`.` to:

```markdown
One migration, `007_events.sql` - `006` is the semantic-recall migration,
which shipped first. It is the only part of this design that touches
existing data, so each step is spelled out with what it can and cannot break.
```

Also update the Testing section bullet that reads "created before `006`" to say `007`.

- [ ] **Step 7: Run the full suite and commit**

Run: `uv run pytest`
Expected: PASS, 0 skipped.

```bash
git add -A
git commit -m "Add entry_vectors, and record why it has no ANN index"
```

---

### Task 4: The `Embedder` Protocol and its local implementation

**Files:**
- Create: `src/remem/embed.py`
- Modify: `src/remem/config.py`, `pyproject.toml`
- Test: `tests/test_embed_protocol.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `remem.embed.Embedder` - Protocol with `name: str`, `dim: int`, and `embed(self, texts: list[str]) -> list[list[float]]`.
  - `remem.embed.EmbedderUnavailable(RuntimeError)`.
  - `remem.embed.LocalEmbedder(model_name: str = DEFAULT_EMBED_MODEL)` - the ONNX implementation.
  - `remem.embed.load_embedder(model_name: str) -> Embedder`.
  - `remem.config.Config.embed_model: str` and `.semantic_threshold: float`; `DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"`, `DEFAULT_SEMANTIC_THRESHOLD = 0.55`.

Tasks 5, 6 and 7 depend on these exact names.

- [ ] **Step 1: Write the failing test**

Create `tests/test_embed_protocol.py`:

```python
"""The embedder seam.

Mirrors store.py: a Protocol with one implementation shipped. The tests here
use a fake, deliberately - a test that downloads a 130MB ONNX model is a test
that fails on a plane, and what needs proving at this layer is the contract,
not the arithmetic of a particular model.
"""

import pytest

from remem.embed import Embedder, EmbedderUnavailable, load_embedder


class FakeEmbedder:
    """Deterministic and dimensionally honest. Not a good embedder - a
    correct one, in the only sense the callers care about."""

    name = "fake-2"
    dim = 2

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t)), 1.0] for t in texts]


def test_fake_satisfies_the_protocol():
    # Protocol is runtime_checkable so services can assert on it.
    assert isinstance(FakeEmbedder(), Embedder)


def test_embed_returns_one_vector_per_text():
    out = FakeEmbedder().embed(["a", "bb", "ccc"])
    assert len(out) == 3
    assert all(len(v) == 2 for v in out)


def test_embed_of_empty_list_is_empty():
    # The batching loop in services/embed.py can legitimately hand over an
    # empty batch; it must not become a model call or an error.
    assert FakeEmbedder().embed([]) == []


def test_unknown_backend_raises_embedder_unavailable():
    with pytest.raises(EmbedderUnavailable) as exc:
        load_embedder("no-such-model-anywhere")
    # The message has to name the install command. This error surfaces in a
    # cron log, where a bare "unavailable" costs an hour.
    assert "uv tool install" in str(exc.value)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_embed_protocol.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'remem.embed'`

- [ ] **Step 3: Write `src/remem/embed.py`**

```python
"""The embedding seam. A Protocol, and the one local implementation shipped.

Same shape as store.py and for the same reason: the thing behind this
interface is a plausible future substitution, and naming the interface now
costs nothing while retrofitting one later costs every call site.

The shipped implementation is local and ONNX-based. No API key, no network on
any read path, no per-call cost, and every existing entry backfillable
without asking anyone's permission. A hosted embedder is a configuration
question for someone else's package, not a dependency of this one.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

#: bge-small-en-v1.5: 384 dimensions, ~130MB of ONNX weights, and near the
#: top of the retrieval benchmarks for its size. Chosen for the size, which
#: is what keeps `remem` installable on a laptop without PyTorch.
DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"


@runtime_checkable
class Embedder(Protocol):
    name: str
    """Recorded in entry_vectors.model. Changing it makes every existing
    vector stale rather than wrong - the old rows stay, readable, until
    something deletes them."""

    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbedderUnavailable(RuntimeError):
    """No embedder could be loaded.

    Raised rather than degraded, because the caller is `remem embed`, whose
    entire job this is. Search degrades instead: its semantic tier finds no
    vectors and falls through to trigram, which is the correct behaviour
    there and the wrong behaviour here.
    """


class LocalEmbedder:
    """fastembed over ONNX Runtime. Imported lazily, on purpose.

    fastembed pulls onnxruntime, which is tens of megabytes and takes a
    noticeable moment to import. Every `remem` invocation would pay that -
    including the hooks, which are meant to be invisible - if this were a
    module-level import. It is deferred to first use, which is the embed
    command and the semantic search tier.
    """

    def __init__(self, model_name: str = DEFAULT_EMBED_MODEL) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise EmbedderUnavailable(
                "The local embedder needs the 'embed' extra. Install it with "
                "`uv tool install --editable '.[embed]'` and re-run."
            ) from exc

        try:
            self._model = TextEmbedding(model_name=model_name)
        except Exception as exc:
            # fastembed raises assorted types for an unknown model name, a
            # failed download, and a corrupt cache. They are one situation to
            # the caller, and the model name is the part worth reporting.
            raise EmbedderUnavailable(
                f"Could not load embedding model {model_name!r}: {exc}. "
                "If this is a name typo, fix REMEM_EMBED_MODEL; if it is a "
                "download failure, re-run `remem embed` when online. "
                "Install the extra with `uv tool install --editable '.[embed]'`."
            ) from exc

        self.name = model_name
        # Asked of the model rather than hardcoded: a wrong constant here
        # would not fail until pgvector rejected the dimension, several
        # layers away from the mistake.
        self.dim = len(next(iter(self._model.embed(["dimension probe"]))))

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            # A model call on an empty batch is wasted startup at best and an
            # error at worst, and the batching loop legitimately produces one.
            return []
        return [list(map(float, v)) for v in self._model.embed(texts)]


def load_embedder(model_name: str = DEFAULT_EMBED_MODEL) -> Embedder:
    """The one place that decides which implementation to build.

    A single branch today. It exists so that adding a second implementation
    is a change here and nowhere else.
    """
    return LocalEmbedder(model_name)
```

- [ ] **Step 4: Add the optional dependency**

In `pyproject.toml`, after the `dependencies` list:

```toml
# Optional, not a hard dependency. onnxruntime is tens of megabytes, and a
# user who only writes and reads entries never needs it. `remem embed` says
# how to install it when it is missing; search degrades to two tiers.
[project.optional-dependencies]
embed = ["fastembed>=0.7.0"]
```

- [ ] **Step 5: Add the config keys**

In `src/remem/config.py`, add near the other defaults:

```python
DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"

# Cosine similarity, so 1.0 is identical and 0 is unrelated. 0.55 is the
# floor at which bge-small stops returning things a reader would call
# related. Too low and the semantic tier answers every query with its four
# nearest entries, which is worse than answering nothing: an empty result
# says "not stored", while four irrelevant ones say "stored, and this is
# what we have".
DEFAULT_SEMANTIC_THRESHOLD = 0.55
```

Add `embed_model: str` and `semantic_threshold: float` to the `Config` dataclass, and in `load()`, alongside the existing threshold handling:

```python
    embed_model = str(
        pick("REMEM_EMBED_MODEL", "embed_model", DEFAULT_EMBED_MODEL)
    ).strip()
    if not embed_model:
        embed_model = DEFAULT_EMBED_MODEL

    semantic = pick("REMEM_SEMANTIC_THRESHOLD", "semantic_threshold",
                    DEFAULT_SEMANTIC_THRESHOLD)
    try:
        semantic = float(semantic)
    except (TypeError, ValueError):
        semantic = DEFAULT_SEMANTIC_THRESHOLD
    if not 0.0 < semantic <= 1.0:
        # Same reasoning as fuzzy_threshold: outside this range the setting is
        # meaningless, and falling back beats silently disabling the tier.
        semantic = DEFAULT_SEMANTIC_THRESHOLD
```

Pass both into the returned `Config(...)`.

- [ ] **Step 6: Register both keys as settable**

In `src/remem/services/settings.py`, alongside the `REMEM_FUZZY_THRESHOLD` entry, add entries for `REMEM_EMBED_MODEL` ("Embedding model recorded in entry_vectors.model.") and `REMEM_SEMANTIC_THRESHOLD` ("Cosine similarity floor for the semantic tier (0 < t <= 1)."), matching the surrounding structure exactly.

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/test_embed_protocol.py tests/test_config.py tests/test_settings_values.py -v`
Expected: PASS, 0 skipped.

- [ ] **Step 8: Prove the real embedder works, once, by hand**

```bash
uv sync --extra embed
uv run python -c "
from remem.embed import load_embedder
e = load_embedder()
v = e.embed(['config command', 'settings subcommand', 'postgres migration'])
print(e.name, e.dim, len(v))
"
```

Expected: `BAAI/bge-small-en-v1.5 384 3`, after a one-time model download.

This is a manual step and stays one. An automated test that downloads a model is a test that fails offline and in CI, and what it would prove - that fastembed works - is not this codebase's claim to make.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "Add the Embedder seam and a local ONNX implementation"
```

---

### Task 5: Store operations for vectors

**Files:**
- Modify: `src/remem/store.py`, `src/remem/backends/postgres/store.py`
- Test: `tests/test_semantic_search.py`

**Interfaces:**
- Consumes: `_entry_filters()` from Task 2, `Match` from Task 1, `entry_vectors` from Task 3.
- Produces, on `Store` and `PostgresStore`:
  - `put_vector(entry_id: UUID, model: str, dim: int, vector: list[float], owner_id: UUID) -> None`
  - `entries_missing_vectors(owner_id: UUID, model: str, limit: int) -> list[Entry]`
  - `semantic_search(query: Query, owner_id: UUID, vector: list[float], model: str, threshold: float) -> list[Hit]`

Task 6 uses the first two, Task 7 the third.

- [ ] **Step 1: Write the failing test**

Create `tests/test_semantic_search.py`:

```python
"""The semantic tier, tested with hand-written vectors.

No model runs here. The question at this layer is whether the SQL ranks by
cosine distance, respects the same filters as every other read, and refuses
to cross owners - none of which depends on the numbers being real embeddings.
Hand-written 2-dimensional vectors make the expected ordering something a
reader can verify by eye.
"""

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Entry, Kind, Match, Query, new_id

pytestmark = pytest.mark.db

MODEL = "test-2d"


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


def _entry(store, owner_id, title, body="body", **kw):
    return store.put_entry(Entry(id=new_id(), kind=Kind.MEMORY, title=title,
                                 body=body, owner_id=owner_id, **kw))


def test_ranks_by_cosine_similarity(store):
    owner = store.ensure_principal("sem-rank")
    near = _entry(store, owner.id, "near")
    far = _entry(store, owner.id, "far")
    store.put_vector(near.id, MODEL, 2, [1.0, 0.0], owner.id)
    store.put_vector(far.id, MODEL, 2, [0.0, 1.0], owner.id)

    hits = store.semantic_search(Query(text="q", limit=10), owner.id,
                                 [1.0, 0.0], MODEL, threshold=0.1)

    assert [h.entry.id for h in hits] == [near.id]  # far is below threshold
    assert hits[0].match is Match.SEMANTIC
    assert hits[0].rank == pytest.approx(1.0)


def test_threshold_excludes_weak_matches(store):
    owner = store.ensure_principal("sem-threshold")
    entry = _entry(store, owner.id, "orthogonal")
    store.put_vector(entry.id, MODEL, 2, [0.0, 1.0], owner.id)

    hits = store.semantic_search(Query(text="q", limit=10), owner.id,
                                 [1.0, 0.0], MODEL, threshold=0.5)
    assert hits == []


def test_never_crosses_owners(store):
    mine = store.ensure_principal("sem-mine")
    yours = store.ensure_principal("sem-yours")
    theirs = _entry(store, yours.id, "not yours")
    store.put_vector(theirs.id, MODEL, 2, [1.0, 0.0], yours.id)

    hits = store.semantic_search(Query(text="q", limit=10), mine.id,
                                 [1.0, 0.0], MODEL, threshold=0.1)
    assert hits == []


def test_applies_the_same_filters_as_other_tiers(store):
    owner = store.ensure_principal("sem-filters")
    a = _entry(store, owner.id, "in project", project="alpha")
    b = _entry(store, owner.id, "other project", project="beta")
    for e in (a, b):
        store.put_vector(e.id, MODEL, 2, [1.0, 0.0], owner.id)

    hits = store.semantic_search(Query(text="q", project="alpha", limit=10),
                                 owner.id, [1.0, 0.0], MODEL, threshold=0.1)
    assert [h.entry.id for h in hits] == [a.id]


def test_ignores_vectors_from_another_model(store):
    # Two models coexisting is the normal state during a re-embed. A search
    # must see exactly one of them, or the ranking is comparing numbers from
    # different spaces - which produces plausible nonsense rather than an
    # error.
    owner = store.ensure_principal("sem-model")
    entry = _entry(store, owner.id, "old model only")
    store.put_vector(entry.id, "other-model", 2, [1.0, 0.0], owner.id)

    hits = store.semantic_search(Query(text="q", limit=10), owner.id,
                                 [1.0, 0.0], MODEL, threshold=0.1)
    assert hits == []


def test_entries_missing_vectors_lists_only_unembedded(store):
    owner = store.ensure_principal("sem-missing")
    done = _entry(store, owner.id, "already embedded")
    todo = _entry(store, owner.id, "not yet")
    store.put_vector(done.id, MODEL, 2, [1.0, 0.0], owner.id)

    missing = store.entries_missing_vectors(owner.id, MODEL, limit=10)
    assert [e.id for e in missing] == [todo.id]


def test_entries_missing_vectors_is_per_model(store):
    # Changing model makes every entry need work again. That is the point of
    # keying on model, and it is what makes a re-embed a normal operation
    # rather than a migration.
    owner = store.ensure_principal("sem-missing-model")
    entry = _entry(store, owner.id, "embedded by the old model")
    store.put_vector(entry.id, "old-model", 2, [1.0, 0.0], owner.id)

    missing = store.entries_missing_vectors(owner.id, "new-model", limit=10)
    assert [e.id for e in missing] == [entry.id]


def test_put_vector_refuses_another_owners_entry(store):
    from remem.store import NotOwner

    mine = store.ensure_principal("put-mine")
    yours = store.ensure_principal("put-yours")
    theirs = _entry(store, yours.id, "not yours")

    with pytest.raises(NotOwner):
        store.put_vector(theirs.id, MODEL, 2, [1.0, 0.0], mine.id)


def test_put_vector_replaces_the_row_for_the_same_model(store):
    owner = store.ensure_principal("put-replace")
    entry = _entry(store, owner.id, "re-embedded")
    store.put_vector(entry.id, MODEL, 2, [1.0, 0.0], owner.id)
    store.put_vector(entry.id, MODEL, 2, [0.0, 1.0], owner.id)

    rows = store._conn.execute(
        "select vector from entry_vectors where entry_id = %s", (entry.id,)
    ).fetchall()
    assert len(rows) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_semantic_search.py -v`
Expected: FAIL with `AttributeError: 'PostgresStore' object has no attribute 'put_vector'`

- [ ] **Step 3: Add the three methods to the Protocol**

In `src/remem/store.py`, add to the entries section, and add `Match` to the imports if the file needs it (it does not - only `Hit` is referenced):

```python
    # vectors
    def put_vector(
        self, entry_id: UUID, model: str, dim: int,
        vector: list[float], owner_id: UUID,
    ) -> None: ...
    def entries_missing_vectors(
        self, owner_id: UUID, model: str, limit: int
    ) -> list[Entry]: ...
    def semantic_search(
        self, query: Query, owner_id: UUID, vector: list[float],
        model: str, threshold: float,
    ) -> list[Hit]: ...
```

- [ ] **Step 4: Implement them in Postgres**

In `src/remem/backends/postgres/store.py`, add `Match` to the `remem.domain` import if Task 1 did not, and add a module-level helper above the class:

```python
def _vector_literal(vector: list[float]) -> str:
    """pgvector's text input format.

    Passed as a string and cast in SQL rather than adding the pgvector-python
    adapter: one more dependency for one type, when the literal form is
    stable, documented, and two lines. Revisit if vectors ever need reading
    back into Python, which no current caller does.
    """
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"
```

Then, after `fuzzy_search()`:

```python
    def put_vector(
        self, entry_id: UUID, model: str, dim: int,
        vector: list[float], owner_id: UUID,
    ) -> None:
        """Insert or replace one entry's vector for one model.

        Ownership is checked here, inside the store, like every other write:
        a caller that could write vectors for another principal's entries
        would be an integrity hole no service check could close.
        """
        with self._cur() as cur:
            cur.execute(
                "select 1 from entries where id = %s and owner_id = %s",
                (entry_id, owner_id),
            )
            if cur.fetchone() is None:
                raise NotOwner(
                    f"entry {entry_id} does not belong to {owner_id}"
                )
            cur.execute(
                """
                insert into entry_vectors (entry_id, model, dim, vector)
                values (%(entry_id)s, %(model)s, %(dim)s, %(vector)s::vector)
                on conflict (entry_id, model) do update
                   set vector = excluded.vector,
                       dim = excluded.dim,
                       created_at = clock_timestamp()
                """,
                {"entry_id": entry_id, "model": model, "dim": dim,
                 "vector": _vector_literal(vector)},
            )

    def entries_missing_vectors(
        self, owner_id: UUID, model: str, limit: int
    ) -> list[Entry]:
        """Entries with no vector for this model, oldest first.

        Oldest first so a long backlog makes steady, resumable progress
        rather than re-visiting the same recent rows on every run.

        Superseded entries are skipped: they are invisible to every search
        tier, so embedding them is work whose result nothing can return.
        """
        with self._cur() as cur:
            cur.execute(
                f"""
                select {entry_columns("e")}
                from entries e
                left join entry_vectors v
                       on v.entry_id = e.id and v.model = %(model)s
                where e.owner_id = %(owner_id)s
                  and e.superseded_by is null
                  and v.entry_id is null
                order by e.created_at asc
                limit %(limit)s
                """,
                {"owner_id": owner_id, "model": model, "limit": limit},
            )
            return [_row_to_entry(r) for r in cur.fetchall()]

    def semantic_search(
        self, query: Query, owner_id: UUID, vector: list[float],
        model: str, threshold: float,
    ) -> list[Hit]:
        """Nearest neighbours by cosine similarity, above a floor.

        `<=>` is pgvector's cosine DISTANCE, so similarity is 1 - distance.
        Reported as similarity because that is the direction every other tier
        ranks in, and a mixed convention across tiers is how a comparison
        silently inverts.

        Exact search, no index - see 006_vectors.sql for why, and for when
        that stops being the right answer.

        The join is inner: an entry with no vector for this model is
        invisible here and reachable by the other two tiers. That is the
        correct degradation - an un-embedded entry is not lost, only less
        findable, and `remem embed` fixes it.
        """
        where, params = _entry_filters(query, owner_id)
        params["vector"] = _vector_literal(vector)
        params["model"] = model
        params["threshold"] = threshold

        similarity = "1 - (v.vector <=> %(vector)s::vector)"
        where.append("v.model = %(model)s")
        where.append(f"{similarity} >= %(threshold)s")

        sql = f"""
            select {entry_columns("e")},
                   {similarity} as rank,
                   left(e.body, 200) as snippet
            from entries e
            join entry_vectors v on v.entry_id = e.id
            where {" and ".join(where)}
            order by rank desc, e.created_at desc
            limit %(limit)s
        """
        with self._cur() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            Hit(entry=_row_to_entry(r), rank=float(r["rank"]),
                snippet=r["snippet"], match=Match.SEMANTIC)
            for r in rows
        ]
```

Note: `semantic_search` takes no snippet from `ts_headline` - there is no tsquery to highlight against, since nothing matched lexically. A leading extract of the body is the honest thing to show.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_semantic_search.py -v`
Expected: PASS, 9 tests, 0 skipped.

If `test_put_vector_replaces_the_row_for_the_same_model` fails on `store._conn`, check the attribute name `PostgresStore` uses for its connection and use that - the test reaches into the store deliberately here, because the assertion is about rows the public interface does not expose.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "Add vector storage and cosine search to the store"
```

---

### Task 6: `remem embed`

**Files:**
- Create: `src/remem/services/embed.py`
- Modify: `src/remem/cli.py`
- Test: `tests/test_embed_service.py`

**Interfaces:**
- Consumes: `Embedder` (Task 4), `put_vector`/`entries_missing_vectors` (Task 5).
- Produces: `remem.services.embed.backfill(store, owner_id, embedder, batch_size=32, max_entries=None) -> EmbedResult`, where `EmbedResult` is a frozen dataclass with `embedded: int`, `failed: int`, `model: str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_embed_service.py`:

```python
"""The embed backlog policy.

Uses a fake embedder throughout. What is being tested is batching,
idempotency and what happens when the model chokes on one batch - none of
which needs real embeddings, all of which needs to be exercised.
"""

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Entry, Kind, new_id
from remem.services.embed import backfill

pytestmark = pytest.mark.db


class FakeEmbedder:
    name = "fake-2"
    dim = 2

    def __init__(self):
        self.batches: list[list[str]] = []

    def embed(self, texts):
        self.batches.append(list(texts))
        return [[float(len(t)), 1.0] for t in texts]


class BrokenEmbedder(FakeEmbedder):
    def embed(self, texts):
        raise RuntimeError("model exploded")


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


def _entries(store, owner_id, n):
    return [
        store.put_entry(Entry(id=new_id(), kind=Kind.MEMORY, title=f"t{i}",
                              body=f"body {i}", owner_id=owner_id))
        for i in range(n)
    ]


def test_embeds_every_entry_that_needs_it(store):
    owner = store.ensure_principal("embed-all")
    _entries(store, owner.id, 3)

    result = backfill(store, owner.id, FakeEmbedder())

    assert result.embedded == 3
    assert result.failed == 0
    assert store.entries_missing_vectors(owner.id, "fake-2", limit=10) == []


def test_is_idempotent(store):
    # Safe to re-run is the whole reason this is a cron command rather than a
    # one-time script.
    owner = store.ensure_principal("embed-twice")
    _entries(store, owner.id, 2)

    backfill(store, owner.id, FakeEmbedder())
    second = backfill(store, owner.id, FakeEmbedder())

    assert second.embedded == 0


def test_batches_rather_than_one_call_per_entry(store):
    owner = store.ensure_principal("embed-batch")
    _entries(store, owner.id, 5)
    embedder = FakeEmbedder()

    backfill(store, owner.id, embedder, batch_size=2)

    assert [len(b) for b in embedder.batches] == [2, 2, 1]


def test_embeds_title_and_body_together(store):
    # Titles carry most of the signal in a short entry, and a body-only
    # embedding makes "config command" fail to match an entry titled exactly
    # that. Both, joined, or the tier misses its most obvious cases.
    owner = store.ensure_principal("embed-text")
    store.put_entry(Entry(id=new_id(), kind=Kind.MEMORY, title="the title",
                          body="the body", owner_id=owner.id))
    embedder = FakeEmbedder()

    backfill(store, owner.id, embedder)

    assert "the title" in embedder.batches[0][0]
    assert "the body" in embedder.batches[0][0]


def test_a_failing_batch_is_counted_not_raised(store):
    # A backlog of hundreds must not be abandoned because one batch failed,
    # and the count is what makes the failure visible in a cron log.
    owner = store.ensure_principal("embed-fail")
    _entries(store, owner.id, 2)

    result = backfill(store, owner.id, BrokenEmbedder())

    assert result.embedded == 0
    assert result.failed == 2


def test_max_entries_bounds_the_run(store):
    owner = store.ensure_principal("embed-bounded")
    _entries(store, owner.id, 5)

    result = backfill(store, owner.id, FakeEmbedder(), batch_size=2,
                      max_entries=3)

    assert result.embedded == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_embed_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'remem.services.embed'`

- [ ] **Step 3: Write the service**

Create `src/remem/services/embed.py`:

```python
"""Embedding backlog policy: what needs work, in what batches, and what to do
when a batch fails."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from remem.domain import Entry
from remem.embed import Embedder
from remem.store import Store

DEFAULT_BATCH_SIZE = 32


@dataclass(frozen=True, slots=True)
class EmbedResult:
    embedded: int
    failed: int
    model: str


def embed_text(entry: Entry) -> str:
    """What actually gets embedded.

    Title and body together. In a short entry the title carries most of the
    signal, and a body-only embedding fails to match a query that is almost
    word-for-word the title - the single most likely query there is. Tags are
    left out: they are already weighted in the full-text tier, and a bag of
    slugs dilutes a sentence embedding rather than sharpening it.
    """
    return f"{entry.title}\n\n{entry.body}"


def backfill(
    store: Store,
    owner_id: UUID,
    embedder: Embedder,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_entries: int | None = None,
) -> EmbedResult:
    """Embed every entry lacking a vector for the embedder's model.

    Idempotent, and safe to re-run: the work is defined by what is missing,
    not by a cursor or a queue, so an interrupted run simply leaves a shorter
    backlog for the next one.

    A batch that raises is counted and skipped rather than propagated. The
    caller is a cron command with a backlog possibly in the hundreds, and
    abandoning all of it because one batch upset the model is the wrong
    trade. The count is what makes the failure visible.
    """
    embedded = 0
    failed = 0
    remaining = max_entries

    while True:
        want = batch_size if remaining is None else min(batch_size, remaining)
        if want <= 0:
            break
        batch = store.entries_missing_vectors(owner_id, embedder.name, want)
        if not batch:
            break

        try:
            vectors = embedder.embed([embed_text(e) for e in batch])
        except Exception:
            failed += len(batch)
            # Stop rather than continue: entries_missing_vectors would hand
            # back this very batch again on the next pass, and a model that
            # fails once fails the same way in a loop. The next scheduled run
            # is the retry.
            break

        for entry, vector in zip(batch, vectors, strict=True):
            store.put_vector(entry.id, embedder.name, len(vector), vector,
                             owner_id)
            embedded += 1

        if remaining is not None:
            remaining -= len(batch)

    return EmbedResult(embedded=embedded, failed=failed, model=embedder.name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_embed_service.py -v`
Expected: PASS, 6 tests, 0 skipped.

- [ ] **Step 5: Add the CLI command**

In `src/remem/cli.py`, near the other top-level commands:

```python
@app.command()
def embed(
    limit: Annotated[Optional[int], typer.Option(
        "--limit", help="Stop after this many entries.")] = None,
    batch: Annotated[int, typer.Option("--batch")] = 32,
):
    """Embed entries that have no vector for the configured model.

    Idempotent and safe to re-run - it does whatever is missing. Run it after
    writing entries, or from cron. Changing REMEM_EMBED_MODEL makes every
    entry need embedding again; the old vectors stay until deleted.
    """
    try:
        embedder = load_embedder(cfg_model := load().embed_model)
    except EmbedderUnavailable as exc:
        # Loud, not fail-soft: embedding is this command's entire job, and a
        # silent success would leave search quietly missing a tier forever.
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    with _session() as s:
        result = backfill(s.store, s.owner.id, embedder,
                          batch_size=batch, max_entries=limit)

    typer.echo(f"Embedded {result.embedded} entries with {result.model}.")
    if result.failed:
        typer.echo(f"{result.failed} failed - re-run to retry.", err=True)
        raise typer.Exit(1)
```

Add the imports: `from remem.embed import EmbedderUnavailable, load_embedder`, `from remem.services.embed import backfill`, and `from remem.config import load`.

Note the unused-looking `cfg_model` walrus is not needed - write it as `load_embedder(load().embed_model)`. Keep it simple.

- [ ] **Step 6: Verify the command end to end**

```bash
uv sync --extra embed
uv run remem embed --limit 5
uv run remem embed
```

Expected: the first reports up to 5 embedded, the second reports 0 - which is the idempotency claim, checked against the real database rather than a fixture.

- [ ] **Step 7: Run the full suite and commit**

Run: `uv run pytest`
Expected: PASS, 0 skipped.

```bash
git add -A
git commit -m "Add remem embed, which fills the vector backlog"
```

---

### Task 7: The three-tier search

The last task, and the one that makes everything before it visible. Nothing above changed a single search result.

**Files:**
- Modify: `src/remem/services/search.py`
- Test: `tests/test_services_search.py` (extend)

**Interfaces:**
- Consumes: everything from Tasks 1, 4, 5.
- Produces: `find()` gains `embedder: Embedder | None = None` and `semantic_threshold: float = DEFAULT_SEMANTIC_THRESHOLD`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_services_search.py`:

```python
"""Three tiers, tried in order, never blended."""

from remem.domain import Match
from remem.services.search import find


class StubStore:
    """Records which tiers were called, and answers with what it was told to.

    A stub rather than a database, because the question here is tier
    ORDERING - a policy decision that lives in the service - and a real store
    would make the test about SQL instead.
    """

    def __init__(self, exact=None, semantic=None, fuzzy=None):
        self._exact = exact or []
        self._semantic = semantic or []
        self._fuzzy = fuzzy or []
        self.called: list[str] = []

    def search(self, query, owner_id):
        self.called.append("exact")
        return list(self._exact)

    def semantic_search(self, query, owner_id, vector, model, threshold):
        self.called.append("semantic")
        return list(self._semantic)

    def fuzzy_search(self, query, owner_id, threshold):
        self.called.append("fuzzy")
        return list(self._fuzzy)


class StubEmbedder:
    name = "stub"
    dim = 2

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


def _hit(match=Match.EXACT):
    from remem.domain import Entry, Hit, Kind, new_id
    e = Entry(id=new_id(), kind=Kind.MEMORY, title="t", body="b",
              owner_id=new_id())
    return Hit(entry=e, rank=1.0, snippet="s", match=match)


def test_exact_results_stop_the_chain(owner_id_factory=None):
    from remem.domain import Query
    store = StubStore(exact=[_hit()], semantic=[_hit(Match.SEMANTIC)])
    from remem.domain import new_id

    hits = find(store, new_id(), Query(text="q"), embedder=StubEmbedder())

    assert store.called == ["exact"]
    assert hits[0].match is Match.EXACT


def test_semantic_runs_only_when_exact_is_empty():
    from remem.domain import Query, new_id
    store = StubStore(semantic=[_hit(Match.SEMANTIC)], fuzzy=[_hit(Match.FUZZY)])

    hits = find(store, new_id(), Query(text="q"), embedder=StubEmbedder())

    assert store.called == ["exact", "semantic"]
    assert [h.match for h in hits] == [Match.SEMANTIC]


def test_fuzzy_runs_only_when_both_above_are_empty():
    from remem.domain import Query, new_id
    store = StubStore(fuzzy=[_hit(Match.FUZZY)])

    hits = find(store, new_id(), Query(text="q"), embedder=StubEmbedder())

    assert store.called == ["exact", "semantic", "fuzzy"]
    assert [h.match for h in hits] == [Match.FUZZY]


def test_no_embedder_degrades_to_two_tiers():
    # The documented degradation: without the optional dependency installed,
    # search still works and simply skips the middle tier. It must not error.
    from remem.domain import Query, new_id
    store = StubStore(fuzzy=[_hit(Match.FUZZY)])

    hits = find(store, new_id(), Query(text="q"), embedder=None)

    assert store.called == ["exact", "fuzzy"]
    assert [h.match for h in hits] == [Match.FUZZY]


def test_an_embedder_that_raises_degrades_rather_than_failing_the_search():
    # A missing model file must cost the semantic tier, not the search. The
    # user asked a question; two tiers can still answer it.
    from remem.domain import Query, new_id

    class Broken(StubEmbedder):
        def embed(self, texts):
            raise RuntimeError("no model")

    store = StubStore(fuzzy=[_hit(Match.FUZZY)])
    hits = find(store, new_id(), Query(text="q"), embedder=Broken())

    assert store.called == ["exact", "semantic", "fuzzy"] or \
           store.called == ["exact", "fuzzy"]
    assert [h.match for h in hits] == [Match.FUZZY]


def test_empty_query_text_skips_both_fallbacks():
    # A listing query - no text, just filters. There is nothing to be
    # approximately like.
    from remem.domain import Query, new_id
    store = StubStore(semantic=[_hit(Match.SEMANTIC)], fuzzy=[_hit(Match.FUZZY)])

    hits = find(store, new_id(), Query(text="  "), embedder=StubEmbedder())

    assert store.called == ["exact"]
    assert hits == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_services_search.py -v`
Expected: FAIL with `TypeError: find() got an unexpected keyword argument 'embedder'`

- [ ] **Step 3: Rewrite `find()`**

In `src/remem/services/search.py`, add imports:

```python
from remem.config import DEFAULT_FUZZY_THRESHOLD, DEFAULT_SEMANTIC_THRESHOLD
from remem.domain import Hit, Match, Origin, Query
from remem.embed import Embedder
```

Replace `find()`:

```python
def find(
    store: Store,
    owner_id: UUID,
    query: Query,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    include_handoffs: bool = False,
    embedder: Embedder | None = None,
    semantic_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
) -> list[Hit]:
    """Three tiers, each running only when the one above returned nothing.

        exact full-text  ->  semantic  ->  trigram

    Fallback rather than blending, for the reason the two-tier version
    already documented: mixing approximate hits into a result set that
    contains exact ones trades precision for a problem that does not exist
    there. With three tiers that matters more, not less - a result set
    holding all three kinds would need the caller to reason about which
    ranking each number came from, and they are not comparable.

    Semantic sits above trigram because meaning beats spelling. A query that
    matches nothing lexically is far more often a different wording than a
    typo, and trigram remains what it always was: the typo net, tried last.

    `embedder` is optional and its absence is not an error. The local
    embedder is an optional dependency; without it search degrades to the two
    tiers it has always had. Same for an embedder that fails at query time -
    the user asked a question, and two tiers can still answer it.
    """
    if query.limit <= 0:
        return []
    if not include_handoffs and not query.origins:
        # An explicit origins list is the caller saying exactly what they
        # want, and is never overridden.
        query = replace(query, origins=list(DEFAULT_ORIGINS))
    query = _clamped(query)

    hits = store.search(query, owner_id)
    text = (query.text or "").strip()
    if hits or not text:
        # No text means a listing query - filters only. There is nothing for
        # the fallbacks to be approximately like.
        return hits

    hits = _semantic(store, owner_id, query, text, embedder, semantic_threshold)
    if hits:
        return hits

    return store.fuzzy_search(query, owner_id, fuzzy_threshold)


def _semantic(
    store: Store,
    owner_id: UUID,
    query: Query,
    text: str,
    embedder: Embedder | None,
    threshold: float,
) -> list[Hit]:
    """The middle tier, and everything that can go wrong with it.

    Kept separate so the degradation reads as one idea rather than three
    try/excepts inside the tier chain. Every failure here means the same
    thing to the caller: no semantic results, carry on to trigram.
    """
    if embedder is None:
        return []
    try:
        vectors = embedder.embed([text])
    except Exception:
        # A missing model file, a corrupt download, an out-of-memory ONNX
        # session. All of them cost this tier and none of them should cost
        # the search. Deliberately broad: the failure modes of a model
        # runtime are not enumerable, and the response is the same for all.
        return []
    if not vectors:
        return []
    return store.semantic_search(query, owner_id, vectors[0], embedder.name,
                                 threshold)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_services_search.py -v`
Expected: PASS, 0 skipped.

- [ ] **Step 5: Wire the embedder into both frontends**

In `src/remem/cli.py`'s `search()`, inside the `with _session() as s:` block, build the embedder before calling `find()`:

```python
    with _session() as s:
        # Best effort. Search without the optional dependency installed is
        # two tiers, not an error - so an unavailable embedder is a None, not
        # an exit. `remem embed` is where this is loud.
        try:
            embedder = load_embedder(s.config.embed_model)
        except EmbedderUnavailable:
            embedder = None
        hits = find(
            s.store, s.owner.id,
            Query(text=query, kinds=list(kind or []), project=project,
                  tags=list(tag or []), limit=limit),
            fuzzy_threshold=s.config.fuzzy_threshold,
            include_handoffs=handoff,
            embedder=embedder,
            semantic_threshold=s.config.semantic_threshold,
        )
```

Make the identical change in `src/remem/mcp_server.py`'s `search` tool.

- [ ] **Step 6: Verify against the failure that motivated the design**

The spec's opening example: four entries written about "config command" work, none findable by that phrase.

```bash
uv run remem embed
uv run remem search "config command"
```

Expected: results, marked `~` if they came from the semantic tier. Compare against `git stash`-ing nothing - just note whether the entries the spec described as unfindable now come back. If they do not, that is a real finding about the threshold, not a reason to skip this step. Report the actual output.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest`
Expected: PASS, 0 skipped. Compare the count against the 506 baseline - it should be 506 plus the roughly 30 tests this plan added, with nothing lost.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "Search by meaning, between exact and trigram"
```

---

## Out of scope, and deliberately so

- **`kb.resolve` and context blocks.** Knowledge base rendering does not search; it resolves collection membership. Semantic recall does not touch it, and the spec lists "no change to `handoff`, `kb`, or collections" as a non-goal.
- **Re-embedding on entry update.** An edited entry keeps its old vector until the next `remem embed`, which will not notice because a row exists for that model. This is a real gap and it belongs to the events plan, where entry writes are already being touched - the fix is deleting the vector on update, which is one line in `put_entry` and needs a test about what search returns in between. Recorded here so it is not discovered as a surprise.
- **A second embedder implementation.** The spec's non-goals: "the seam is documented; one implementation ships."
- **The document-corpus import and evaluation.** After the pipeline works, and a data-ownership question before an engineering one.

## Self-Review

**Spec coverage** - checked against the spec's Search, Embedding, and `entry_vectors` sections:

| Spec requirement | Task |
|---|---|
| `entry_vectors` table, separate and disposable | 3 |
| Index question settled | Decisions section, recorded in 3 |
| Embedder is a Protocol with one local implementation | 4 |
| ONNX, no PyTorch | 4 |
| `remem embed`, idempotent, batched | 6 |
| Three tiers, never blended | 7 |
| `Hit.fuzzy` becomes `Hit.match` | 1 |
| Every frontend surfaces the marker | 1, 7 |
| Un-embedded entries reachable by other tiers | 5 (inner join), 7 |
| No embedder: `embed` exits non-zero, search degrades | 6, 7 |

`DEFAULT_ORIGINS` gaining `extracted` is **not** covered here and correctly so - that origin does not exist until the events plan renames it.

**Placeholder scan:** no TBDs, no "add error handling", no "similar to Task N". Every code step carries the code.

**Type consistency:** `Match` (Task 1) is constructed in Tasks 5 and 7 and formatted in Task 1's frontend changes. `_entry_filters` (Task 2) is called in Task 5. `Embedder.name`/`.dim`/`.embed` (Task 4) are used by Tasks 5, 6, 7 under those exact names. `entries_missing_vectors(owner_id, model, limit)` and `put_vector(entry_id, model, dim, vector, owner_id)` (Task 5) are called with that argument order in Task 6. `backfill(...) -> EmbedResult` fields `embedded`/`failed`/`model` (Task 6) are read in the CLI in that same task.

One inconsistency found and fixed while reviewing: Task 6's CLI draft had a stray walrus (`cfg_model := ...`) left over from an earlier shape; the step now says to write it plainly.
