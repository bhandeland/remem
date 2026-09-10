# claude-mem Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `remem import claude-mem <path>` reads a local claude-mem sqlite database and writes its knowledge into remem as entries with `origin='imported'`, re-runnably.

**Architecture:** Three layers, matching the repo's existing seam. `importers/claude_mem.py` is a pure reader - sqlite file in, neutral `SourceRecord`s out, no store access. `services/import_.py` owns every policy decision - kind, origin, identity tags, idempotency, project resolution. `cli.py` parses and formats only.

**Tech Stack:** Python 3.14, stdlib `sqlite3` (no new dependency), Typer, psycopg/Postgres, pytest.

**Spec:** `docs/superpowers/specs/2026-09-09-claude-mem-import-design.md` - read it before Task 1; the reasoning behind each ruling lives there, not here.

## Global Constraints

- **Python 3.14**, `from __future__ import annotations` at the top of every module.
- **Line width 88, and the formatter owns it.** Never hand-edit anything `ruff format` or `ruff check --fix` would fix.
- **`make check` is the only verification command.** It runs `ruff format` -> `ruff check --fix` -> `pyrefly check` -> `pytest` and stops at the first failure.
- **A green pytest run means nothing unless the skip count is zero.** `db`-marked tests skip silently when Postgres is unreachable. Run `docker compose up -d` first and read the skip count.
- **No `print()` anywhere in `src/`.** `T20` is an error with no exemptions - stdout belongs to the MCP protocol. Use `typer.echo` in `cli.py`.
- **Comments explain *why*, at length**, especially where a decision looks arbitrary. Match the surrounding density.
- **In prose, docs and commit messages: spaced hyphens ` - `, never em dashes.**
- **Watch every new guard fail before trusting it.** Revert the defect it targets on a scratch copy, confirm red, and clear `__pycache__` between variants - stale bytecode reports a false pass.
- **Never edit an applied migration.** Add a new numbered file.

---

### Task 1: The `imported` origin

Adds the enum value, the domain member, and the `DEFAULT_ORIGINS` entry. Nothing uses it yet - this task exists on its own because a missing `DEFAULT_ORIGINS` entry makes an origin silently vanish from search, and that is worth its own test and its own review.

**Files:**
- Create: `src/remem/backends/postgres/migrations/019_imported_origin.sql`
- Modify: `src/remem/domain.py` (the `Origin` enum, around line 22-35)
- Modify: `src/remem/services/search.py` (`DEFAULT_ORIGINS`, around line 59)
- Test: `tests/test_imported_origin.py` (new, pure - no `db` marker)

**Interfaces:**
- Consumes: nothing.
- Produces: `Origin.IMPORTED` (value `"imported"`), present in `search.DEFAULT_ORIGINS` and absent from `domain.INJECTED_ORIGINS`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_imported_origin.py`:

```python
"""The imported origin, and the two lists that decide what it means.

Pure - no database. The values are spelled literally rather than derived
from the enum, because a test that builds its expectations out of the module
under test pins nothing.
"""

from __future__ import annotations

from remem.domain import INJECTED_ORIGINS, Origin
from remem.services.search import DEFAULT_ORIGINS


def test_the_imported_origin_exists():
    assert Origin.IMPORTED == "imported"


def test_imported_is_searchable_by_default():
    """DEFAULT_ORIGINS is an allowlist. An origin missing from it does not
    narrow results - it disappears from them entirely, silently."""
    assert Origin.IMPORTED in DEFAULT_ORIGINS


def test_imported_is_not_injected_into_context_blocks():
    """kb.resolve filters the query half to INJECTED_ORIGINS, so absence
    here is what keeps imported machine text from crowding out hand-written
    rules. Same treatment as EXTRACTED."""
    assert Origin.IMPORTED not in INJECTED_ORIGINS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_imported_origin.py --cache-clear -q`
Expected: FAIL - `AttributeError: IMPORTED` on the first test.

- [ ] **Step 3: Add the migration**

Create `src/remem/backends/postgres/migrations/019_imported_origin.sql`:

```sql
-- Knowledge imported from another tool is an ordinary entry with its own
-- origin, so it can be searched, audited and (if an import goes wrong)
-- removed as a set, without a second table.
--
-- Its own value rather than reusing 'extracted': both are machine-written,
-- but 'extracted' means remem's own extractor produced this from events
-- remem itself recorded. An imported entry came from a tool whose judgement
-- remem cannot vouch for and whose store is being decommissioned. Collapsing
-- the two would make a bad import indistinguishable from remem's own work
-- for as long as the row lives.
--
-- This migration adds a value and writes NO row that carries it. That is
-- required, not stylistic: as 005_handoff.sql records and 012_ingest_origins
-- repeats, a value added by `add value` cannot be USED in the transaction
-- that added it unless the type was created there too, and migrate() runs
-- every pending migration in one transaction.
--
-- `if not exists` because a database migrated by a build that already
-- carried this value must not fail here.
alter type entry_origin add value if not exists 'imported';
```

- [ ] **Step 4: Add the domain member**

In `src/remem/domain.py`, inside `class Origin(StrEnum)`, after `ARCHIVED`:

```python
    #: Knowledge loaded from another tool's store by `remem import`. In
    #: DEFAULT_ORIGINS, so it is searchable; deliberately NOT in
    #: INJECTED_ORIGINS, so it never renders into a context block. See
    #: docs/superpowers/specs/2026-09-09-claude-mem-import-design.md.
    IMPORTED = "imported"
```

- [ ] **Step 5: Add it to DEFAULT_ORIGINS**

In `src/remem/services/search.py`, extend the list and the comment above it:

```python
DEFAULT_ORIGINS = [
    Origin.HUMAN,
    Origin.AGENT,
    Origin.EXTRACTED,
    Origin.INGESTED,
    Origin.IMPORTED,
]
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_imported_origin.py --cache-clear -q`
Expected: 3 passed.

- [ ] **Step 7: Prove the guards bite**

Remove `Origin.IMPORTED` from `DEFAULT_ORIGINS`, clear `__pycache__`, re-run: `test_imported_is_searchable_by_default` must go red. Then add `Origin.IMPORTED` to `INJECTED_ORIGINS`, clear `__pycache__`, re-run: `test_imported_is_not_injected_into_context_blocks` must go red. Restore both.

```bash
find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null
```

- [ ] **Step 8: Apply the migration and run the full suite**

```bash
docker compose up -d
remem db up
remem db status
make check
```

Expected: `db status` lists 019 as applied; `make check` green with **zero** skips.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "Add the imported origin"
```

---

### Task 2: `SourceRecord` and the modern claude-mem reader

The pure reader for schema v33, where everything lives in one `memory_items` table. Legacy support is Task 3 - split because a reviewer can reasonably accept the modern path and reject the legacy mapping.

**Files:**
- Create: `src/remem/importers/__init__.py`
- Create: `src/remem/importers/base.py`
- Create: `src/remem/importers/claude_mem.py`
- Test: `tests/test_claude_mem_reader.py` (new, pure - no `db` marker)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `importers.base.SourceKind` - `StrEnum` with `OBSERVATION`, `SUMMARY`, `PROMPT`, `MANUAL`.
  - `importers.base.SourceRecord` - frozen slots dataclass: `source_id: str`, `kind: SourceKind`, `project: str | None`, `title: str`, `summary: str | None`, `body: str`, `tags: tuple[str, ...]`, `created_at: datetime | None`.
  - `importers.claude_mem.NAMESPACE: Final = "cmem"`.
  - `importers.claude_mem.read(path: Path) -> list[SourceRecord]`.
  - `importers.claude_mem.UnreadableSource(Exception)`.

**Boundary note for the implementer:** the reader renders the body prose. Rendering `facts`/`concepts`/`narrative` is a claude-mem-shaped concern, so it belongs to the claude-mem module; the service must never learn those words. What the *service* decides is kind, origin, identity and idempotency. Keep that line - the Obsidian importer is a second producer of the same `SourceRecord`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_claude_mem_reader.py`:

```python
"""The claude-mem sqlite reader. Pure - builds its own database, no store.

Column names and values are spelled literally, never imported from the
module under test: this is an external contract, and a test that renames
itself alongside the code pins nothing.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from remem.importers.base import SourceKind
from remem.importers.claude_mem import UnreadableSource, read

MODERN_SCHEMA = """
create table projects (
  id text primary key, name text not null, slug text,
  root_path text, metadata text not null default '{}',
  created_at_epoch integer not null, updated_at_epoch integer not null
);
create table memory_items (
  id text primary key, project_id text not null, server_session_id text,
  legacy_observation_id integer,
  kind text not null, type text not null, title text, subtitle text,
  text text, narrative text,
  facts text not null default '[]', concepts text not null default '[]',
  files_read text not null default '[]',
  files_modified text not null default '[]',
  metadata text not null default '{}',
  created_at_epoch integer not null, updated_at_epoch integer not null
);
"""


def _modern(tmp_path: Path) -> Path:
    db = tmp_path / "claude-mem.db"
    conn = sqlite3.connect(db)
    conn.executescript(MODERN_SCHEMA)
    conn.execute(
        "insert into projects values (?,?,?,?,?,?,?)",
        ("p1", "at-workspace", "at-workspace", "/x", "{}", 1782832648, 1782832648),
    )
    conn.execute(
        "insert into memory_items values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "m1", "p1", "s1", None, "observation", "discovery",
            "Workspace technology inventory",
            "Mapped 80+ projects across Python, Node and Terraform",
            None,
            "The user ran a comprehensive scan of their workspace.",
            json.dumps(["Workspace occupies 75GB", "80+ project directories"]),
            json.dumps(["how-it-works: git repos are the unit"]),
            "[]", "[]", "{}", 1782832648, 1782832648,
        ),
    )
    conn.commit()
    conn.close()
    return db


def test_an_observation_becomes_one_record(tmp_path):
    [record] = read(_modern(tmp_path))

    assert record.source_id == "m1"
    assert record.kind is SourceKind.OBSERVATION
    assert record.project == "at-workspace"
    assert record.title == "Workspace technology inventory"


def test_the_subtitle_becomes_the_summary(tmp_path):
    """claude-mem's subtitle is already a one-line hook written to sit under
    a title, which is exactly what remem's summary field is for."""
    [record] = read(_modern(tmp_path))

    assert record.summary == "Mapped 80+ projects across Python, Node and Terraform"


def test_facts_and_concepts_are_rendered_into_the_body(tmp_path):
    [record] = read(_modern(tmp_path))

    assert "comprehensive scan" in record.body
    assert "Workspace occupies 75GB" in record.body
    assert "80+ project directories" in record.body
    assert "how-it-works: git repos are the unit" in record.body


def test_facts_never_become_tags(tmp_path):
    """Tags are the highest-weighted tsvector field after the title, so a
    sentence in a tag distorts ranking for every query sharing a word."""
    [record] = read(_modern(tmp_path))

    assert record.tags == ("cmem-type:discovery",)


def test_a_file_that_is_not_a_database_is_refused_by_name(tmp_path):
    junk = tmp_path / "notes.txt"
    junk.write_text("this is not sqlite")

    with pytest.raises(UnreadableSource, match="notes.txt"):
        read(junk)


def test_a_database_with_no_recognised_tables_names_what_it_looked_for(tmp_path):
    db = tmp_path / "other.db"
    conn = sqlite3.connect(db)
    conn.executescript("create table unrelated (id integer primary key);")
    conn.commit()
    conn.close()

    with pytest.raises(UnreadableSource) as exc:
        read(db)

    assert "memory_items" in str(exc.value)
    assert "observations" in str(exc.value)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_claude_mem_reader.py --cache-clear -q`
Expected: FAIL - `ModuleNotFoundError: No module named 'remem.importers'`.

- [ ] **Step 3: Write `importers/base.py`**

```python
"""What every importer produces, regardless of what it read.

Neutral on purpose: the claude-mem reader is the first producer and the
Obsidian reader is the second, and the service that maps these into entries
must not learn either source's vocabulary. A record carries a body that is
already rendered prose, because HOW a source's fields become prose is that
source's concern - what the service decides is kind, origin, identity and
idempotency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SourceKind(StrEnum):
    """What the source called this record, before remem decides what it is."""

    OBSERVATION = "observation"
    SUMMARY = "summary"
    PROMPT = "prompt"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class SourceRecord:
    #: Stable within the source. Becomes the `<ns>:<source_id>` identity tag,
    #: which is what makes an import re-runnable.
    source_id: str
    kind: SourceKind
    project: str | None
    title: str
    summary: str | None
    body: str
    #: Source-specific extras, already namespaced (e.g. `cmem-type:discovery`).
    #: Never prose - see the reader's rendering rules.
    tags: tuple[str, ...] = ()
    created_at: datetime | None = None
```

- [ ] **Step 4: Write `importers/claude_mem.py` for the modern schema**

Create `src/remem/importers/__init__.py` as an empty module docstring file, then `claude_mem.py`:

```python
"""Reading a claude-mem sqlite database. Pure: a path in, records out.

No store, no policy, no network. The one file it is given is the only thing
it touches, which is what lets its tests build a database in tmp_path and
carry no `db` marker.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from remem.importers.base import SourceKind, SourceRecord

#: Prefixes every tag this importer mints, so an Obsidian import can never
#: collide with a claude-mem one on identity.
NAMESPACE: Final = "cmem"

#: Named in the refusal message when neither schema is present, so a person
#: pointed at the wrong file learns what was actually expected.
MODERN_TABLE: Final = "memory_items"
LEGACY_TABLES: Final = ("observations", "session_summaries", "user_prompts")


class UnreadableSource(Exception):
    """The file is not a claude-mem database, or not a database at all."""


def read(path: Path) -> list[SourceRecord]:
    conn = _open(path)
    try:
        tables = _tables(conn)
        if MODERN_TABLE in tables:
            return _read_modern(conn)
        raise UnreadableSource(
            f"{path} has none of the tables a claude-mem database has. "
            f"Looked for {MODERN_TABLE!r} (schema 33 and later) and "
            f"{', '.join(repr(t) for t in LEGACY_TABLES)} (earlier)."
        )
    finally:
        conn.close()


def _open(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        # connect() is lazy, so a non-database file is only discovered on
        # the first read. Force it here, where the path is still in hand.
        conn.execute("select count(*) from sqlite_master")
    except sqlite3.Error as exc:
        raise UnreadableSource(f"{path} is not readable as sqlite: {exc}") from exc
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("select name from sqlite_master where type='table'")
    return {r[0] for r in rows}


def _read_modern(conn: sqlite3.Connection) -> list[SourceRecord]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "select m.*, p.name as project_name "
        "from memory_items m left join projects p on p.id = m.project_id "
        "order by m.created_at_epoch"
    )
    return [_record(row) for row in rows]


def _record(row: sqlite3.Row) -> SourceRecord:
    kind = SourceKind(row["kind"])
    return SourceRecord(
        source_id=str(row["id"]),
        kind=kind,
        project=row["project_name"],
        title=row["title"] or "(untitled)",
        summary=(row["subtitle"] or None),
        body=_body(row),
        tags=(f"{NAMESPACE}-type:{row['type']}",) if row["type"] else (),
        created_at=_when(row["created_at_epoch"]),
    )


def _body(row: sqlite3.Row) -> str:
    """Prose, not JSON. The body is half the embedding text and the bulk of
    the tsvector, so a serialised array here would be searched as punctuation.
    """
    parts: list[str] = []
    for field in ("narrative", "text"):
        value = (row[field] or "").strip()
        if value:
            parts.append(value)
    for field, heading in (("facts", "Facts"), ("concepts", "Concepts")):
        items = _json_list(row[field])
        if items:
            bullets = "\n".join(f"- {item}" for item in items)
            parts.append(f"## {heading}\n\n{bullets}")
    return "\n\n".join(parts)


def _json_list(raw: str | None) -> list[str]:
    """claude-mem stores these as a JSON array in a TEXT column, and the
    2026-06-30 export proves they are sometimes a JSON string holding a JSON
    array. Anything that does not decode into a list is dropped rather than
    raised on: one malformed column must not end an import."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return [value]
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if str(v).strip()]


def _when(epoch: int | None) -> datetime | None:
    """claude-mem writes epoch milliseconds in some columns and seconds in
    others across versions. Values past the year 2200 are milliseconds."""
    if not epoch:
        return None
    seconds = epoch / 1000 if epoch > 7_258_118_400 else epoch
    return datetime.fromtimestamp(seconds, tz=timezone.utc)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_claude_mem_reader.py --cache-clear -q`
Expected: 6 passed.

- [ ] **Step 6: Prove the guards bite**

Change `_body` to append `json.dumps(items)` instead of bullets - `test_facts_and_concepts_are_rendered_into_the_body` should still pass (the substrings survive), which tells you that test is weaker than it looks. Then change `_record` to put facts into `tags` - `test_facts_never_become_tags` must go red. Clear `__pycache__` between each. Restore.

- [ ] **Step 7: Commit**

```bash
make check
git add -A
git commit -m "Read a modern claude-mem database into neutral records"
```

---

### Task 3: The legacy claude-mem schema

Versions before schema 33 keep three separate tables. The reference export (2026-06-30) is this shape, and the source machine's version is unknown, so both must work.

**Files:**
- Modify: `src/remem/importers/claude_mem.py`
- Test: `tests/test_claude_mem_reader_legacy.py` (new, pure)

**Interfaces:**
- Consumes: `SourceRecord`, `SourceKind`, `read`, `NAMESPACE`, `UnreadableSource` from Task 2.
- Produces: no new public names. `read()` now also accepts the legacy shape.

- [ ] **Step 1: Write the failing test**

Create `tests/test_claude_mem_reader_legacy.py`. Build the fixture from the **real 2026-06-30 export's** column set, spelled literally:

```python
"""The pre-33 claude-mem schema: three tables instead of one.

The column list is copied from a real export (2026-06-30), not from the
current upstream schema - this fixture exists to pin the shape the code has
to survive, and deriving it from today's schema would pin nothing.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from remem.importers.base import SourceKind
from remem.importers.claude_mem import read

LEGACY_SCHEMA = """
create table observations (
  id integer primary key, memory_session_id text, project text,
  text text, type text, title text, subtitle text,
  facts text, concepts text, files_read text, files_modified text,
  narrative text, metadata text, created_at text,
  created_at_epoch integer, prompt_number integer,
  agent_id text, agent_type text, content_hash text,
  discovery_tokens integer, generated_by_model text,
  merged_into_project text, relevance_count integer
);
create table session_summaries (
  id integer primary key, memory_session_id text, project text,
  request text, investigated text, learned text, completed text,
  next_steps text, notes text, files_read text, files_edited text,
  created_at text, created_at_epoch integer, prompt_number integer,
  discovery_tokens integer, merged_into_project text
);
create table user_prompts (
  id integer primary key, session_db_id integer, content_session_id text,
  prompt_number integer, prompt_text text,
  created_at text, created_at_epoch integer
);
"""


def _legacy(tmp_path: Path) -> Path:
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "insert into observations (id, memory_session_id, project, type, title,"
        " subtitle, facts, concepts, narrative, created_at_epoch)"
        " values (?,?,?,?,?,?,?,?,?,?)",
        (
            1, "sess-a", "at-workspace", "discovery",
            "Workspace technology inventory",
            "Mapped 80+ projects",
            json.dumps(["Workspace occupies 75GB"]),
            json.dumps(["how-it-works: git repos are the unit"]),
            "The user ran a comprehensive scan.",
            1782832648000,
        ),
    )
    conn.execute(
        "insert into session_summaries (id, memory_session_id, project, request,"
        " investigated, learned, completed, next_steps, created_at_epoch)"
        " values (?,?,?,?,?,?,?,?,?)",
        (
            1, "sess-a", "at-workspace",
            "User invoked learn-codebase",
            "Scanned all 75+ directories",
            "Workspace is a meta-workspace",
            "Created a topology reference",
            "Session concluded",
            1782832649000,
        ),
    )
    for n, text in ((1, "/claude-mem:learn-codebase"), (2, "now summarise it")):
        conn.execute(
            "insert into user_prompts (id, session_db_id, content_session_id,"
            " prompt_number, prompt_text, created_at_epoch) values (?,?,?,?,?,?)",
            (n, 1, "sess-a", n, text, 1782832650000 + n),
        )
    conn.commit()
    conn.close()
    return db


def _by_kind(records, kind):
    return [r for r in records if r.kind is kind]


def test_all_three_legacy_tables_are_read(tmp_path):
    records = read(_legacy(tmp_path))

    assert len(_by_kind(records, SourceKind.OBSERVATION)) == 1
    assert len(_by_kind(records, SourceKind.SUMMARY)) == 1
    assert len(_by_kind(records, SourceKind.PROMPT)) == 1


def test_a_legacy_observation_maps_like_a_modern_one(tmp_path):
    [obs] = _by_kind(read(_legacy(tmp_path)), SourceKind.OBSERVATION)

    assert obs.source_id == "1"
    assert obs.summary == "Mapped 80+ projects"
    assert obs.tags == ("cmem-type:discovery",)
    assert "Workspace occupies 75GB" in obs.body


def test_a_summary_renders_its_five_fields(tmp_path):
    [summary] = _by_kind(read(_legacy(tmp_path)), SourceKind.SUMMARY)

    for expected in (
        "User invoked learn-codebase",
        "Scanned all 75+ directories",
        "Workspace is a meta-workspace",
        "Created a topology reference",
        "Session concluded",
    ):
        assert expected in summary.body


def test_prompts_are_grouped_into_one_record_per_session(tmp_path):
    """One entry per prompt would be near-empty entries competing in search
    against real memories. The sequence is the signal, not the string."""
    [prompts] = _by_kind(read(_legacy(tmp_path)), SourceKind.PROMPT)

    assert prompts.source_id == "prompts:sess-a"
    assert "/claude-mem:learn-codebase" in prompts.body
    assert "now summarise it" in prompts.body


def test_milliseconds_are_not_read_as_seconds(tmp_path):
    """Legacy epochs are milliseconds. Read as seconds they land in 1970."""
    [obs] = _by_kind(read(_legacy(tmp_path)), SourceKind.OBSERVATION)

    assert obs.created_at is not None
    assert obs.created_at.year == 2026
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_claude_mem_reader_legacy.py --cache-clear -q`
Expected: FAIL - `UnreadableSource` raised, because `memory_items` is absent and the legacy branch does not exist yet.

- [ ] **Step 3: Add the legacy branch**

In `read()`, before the refusal:

```python
        if set(LEGACY_TABLES) & tables:
            return _read_legacy(conn, tables)
```

Then add, after `_read_modern`:

```python
def _read_legacy(conn: sqlite3.Connection, tables: set[str]) -> list[SourceRecord]:
    """Pre-33 claude-mem: three tables, one per record kind.

    Each table is optional - a database that never recorded a prompt simply
    has no `user_prompts`, and refusing that would refuse a valid store.
    """
    conn.row_factory = sqlite3.Row
    records: list[SourceRecord] = []
    if "observations" in tables:
        rows = conn.execute("select * from observations order by created_at_epoch")
        records.extend(_record(row) for row in rows)
    if "session_summaries" in tables:
        rows = conn.execute(
            "select * from session_summaries order by created_at_epoch"
        )
        records.extend(_summary(row) for row in rows)
    if "user_prompts" in tables:
        rows = conn.execute("select * from user_prompts order by id")
        records.extend(_prompts(list(rows)))
    return records
```

The legacy `observations` table has `project`, `type`, `title`, `subtitle`,
`facts`, `concepts`, `narrative` and `text` under those exact names, so
`_record` already reads it - except for `project_name`, which only the modern
join produces. Make `_record` tolerant of both:

```python
def _column(row: sqlite3.Row, *names: str):
    """The first of `names` the row actually has. The modern reader joins
    `projects` and yields `project_name`; the legacy tables carry `project`
    inline. One accessor rather than two record builders."""
    available = row.keys()
    for name in names:
        if name in available:
            return row[name]
    return None
```

and in `_record`, replace the project and kind lines with:

```python
    raw_kind = _column(row, "kind")
    kind = SourceKind(raw_kind) if raw_kind else SourceKind.OBSERVATION
    ...
        project=_column(row, "project_name", "project"),
```

- [ ] **Step 4: Add the summary and prompt builders**

```python
#: The five fields a legacy session summary carries, in the order they are
#: rendered. Spelled here rather than read off the row so that a column added
#: upstream cannot silently reorder a body.
SUMMARY_FIELDS: Final = (
    ("request", "Request"),
    ("investigated", "Investigated"),
    ("learned", "Learned"),
    ("completed", "Completed"),
    ("next_steps", "Next steps"),
)


def _summary(row: sqlite3.Row) -> SourceRecord:
    parts = []
    for field, heading in SUMMARY_FIELDS:
        value = (_column(row, field) or "").strip()
        if value:
            parts.append(f"## {heading}\n\n{value}")
    session = _column(row, "memory_session_id") or row["id"]
    return SourceRecord(
        source_id=f"summary:{row['id']}",
        kind=SourceKind.SUMMARY,
        project=_column(row, "project_name", "project"),
        title=f"Session summary {session}",
        summary=None,
        body="\n\n".join(parts),
        tags=(),
        created_at=_when(_column(row, "created_at_epoch")),
    )


def _prompts(rows: list[sqlite3.Row]) -> list[SourceRecord]:
    """One record per session, not per prompt.

    A single prompt is often a slash command - not knowledge, and one entry
    each would be dozens of near-empty entries competing in search with real
    memories. The ORDERED SEQUENCE of a session's prompts is the signal.
    """
    by_session: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        session = _column(row, "content_session_id") or "unknown"
        by_session.setdefault(str(session), []).append(row)

    records = []
    for session, group in by_session.items():
        lines = []
        for n, row in enumerate(group, start=1):
            text = (_column(row, "prompt_text") or "").strip()
            if text:
                lines.append(f"{n}. {text}")
        if not lines:
            continue
        records.append(
            SourceRecord(
                source_id=f"prompts:{session}",
                kind=SourceKind.PROMPT,
                project=None,
                title=f"Prompts from session {session}",
                summary=None,
                body="\n".join(lines),
                tags=(),
                created_at=_when(_column(group[0], "created_at_epoch")),
            )
        )
    return records
```

- [ ] **Step 5: Run both reader test files**

Run: `uv run pytest tests/test_claude_mem_reader.py tests/test_claude_mem_reader_legacy.py --cache-clear -q`
Expected: 11 passed. Task 2's tests must still pass unchanged - if `_column` broke the modern path, that is where it shows.

- [ ] **Step 6: Prove the guards bite**

Make `_prompts` emit one record per row instead of per session - `test_prompts_are_grouped_into_one_record_per_session` must go red. Make `_when` divide unconditionally by 1000 - `test_milliseconds_are_not_read_as_seconds` must go red (and the modern test with second-epochs should too, which is the point of having both). Clear `__pycache__` between variants. Restore.

- [ ] **Step 7: Commit**

```bash
make check
git add -A
git commit -m "Read the pre-33 claude-mem schema too"
```

---

### Task 4: Mapping records to entries

The service's pure half: what kind each record becomes, what tags it carries, and what a dry run reports. No store yet - Task 5 adds writing.

**Files:**
- Create: `src/remem/services/import_.py`
- Test: `tests/test_import_mapping.py` (new, pure)

**Interfaces:**
- Consumes: `SourceRecord`, `SourceKind` (Task 2); `Origin.IMPORTED` (Task 1).
- Produces:
  - `services.import_.Planned` - frozen slots dataclass: `record: SourceRecord`, `kind: Kind`, `project: str | None`, `tags: list[str]`.
  - `services.import_.Report` - slots dataclass: `created: int = 0`, `updated: int = 0`, `unchanged: int = 0`, `by_kind: dict[str, int]`, `by_project: dict[str, int]`, `dry_run: bool = False`.
  - `services.import_.plan(records, *, namespace, project=None) -> list[Planned]`.
  - `services.import_.KIND_FOR: dict[SourceKind, Kind]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_import_mapping.py`:

```python
"""What an imported record becomes. Pure - no store, no database."""

from __future__ import annotations

from remem.domain import Kind
from remem.importers.base import SourceKind, SourceRecord
from remem.services.import_ import plan


def _record(**kw) -> SourceRecord:
    base = dict(
        source_id="m1",
        kind=SourceKind.OBSERVATION,
        project="at-workspace",
        title="A title",
        summary="A hook",
        body="A body",
        tags=("cmem-type:discovery",),
    )
    return SourceRecord(**{**base, **kw})


def test_an_observation_becomes_a_note():
    [planned] = plan([_record()], namespace="cmem")

    assert planned.kind is Kind.NOTE


def test_a_summary_becomes_a_doc_not_a_handoff():
    """origin=handoff would supersede every prior summary for the same
    project and hide the rest from search. As docs they keep their own
    identity and stay findable."""
    [planned] = plan([_record(kind=SourceKind.SUMMARY)], namespace="cmem")

    assert planned.kind is Kind.DOC


def test_every_record_carries_its_identity_tag():
    """The tag is what makes a second import skip instead of duplicate."""
    [planned] = plan([_record()], namespace="cmem")

    assert "cmem:m1" in planned.tags


def test_source_tags_are_kept_alongside_the_identity_tag():
    [planned] = plan([_record()], namespace="cmem")

    assert "cmem-type:discovery" in planned.tags


def test_the_project_override_wins_over_the_source_project():
    [planned] = plan([_record()], namespace="cmem", project="accutrade")

    assert planned.project == "accutrade"


def test_without_an_override_the_source_project_is_used_verbatim():
    [planned] = plan([_record()], namespace="cmem")

    assert planned.project == "at-workspace"


def test_nothing_imported_is_ever_a_rule():
    """write.remember raises RuleNeedsSummary for a rule with no summary,
    and no imported record is a convention this project agreed to follow."""
    kinds = {
        plan([_record(kind=k)], namespace="cmem")[0].kind for k in SourceKind
    }

    assert Kind.RULE not in kinds
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_import_mapping.py --cache-clear -q`
Expected: FAIL - `ModuleNotFoundError: remem.services.import_`.

- [ ] **Step 3: Write the mapping**

Create `src/remem/services/import_.py`:

```python
"""Importing another tool's knowledge store. Every policy decision is here.

Named `import_` because `import` is a keyword. The readers in
`remem/importers/` know how to turn one source's rows into neutral records;
this module decides what a record becomes, how it is identified, and whether
a second run writes anything at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from remem.domain import Kind
from remem.importers.base import SourceKind, SourceRecord

#: What each source kind becomes. Nothing maps to Kind.RULE: a rule is a
#: convention this project agreed to follow, and no imported record is that.
#: Summaries are DOC and deliberately not origin=handoff - see the design.
KIND_FOR: dict[SourceKind, Kind] = {
    SourceKind.OBSERVATION: Kind.NOTE,
    SourceKind.MANUAL: Kind.NOTE,
    SourceKind.PROMPT: Kind.NOTE,
    SourceKind.SUMMARY: Kind.DOC,
}


@dataclass(frozen=True, slots=True)
class Planned:
    """One record, and what it will become. Produced without a store, so a
    dry run costs nothing and can be tested without Postgres."""

    record: SourceRecord
    kind: Kind
    project: str | None
    tags: list[str]


@dataclass(slots=True)
class Report:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    by_project: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False


def identity_tag(namespace: str, source_id: str) -> str:
    """The tag that makes an import re-runnable, playing exactly the role
    `src:`/`sec:` plays for ingest."""
    return f"{namespace}:{source_id}"


def plan(
    records: list[SourceRecord],
    *,
    namespace: str,
    project: str | None = None,
) -> list[Planned]:
    """Decide what each record becomes. No store, no writes.

    `project` forces every record into one project. Without it the source's
    own project name is used verbatim: neither side derives the other, so
    the mapping is stated rather than guessed, and the dry run prints the
    distinct names before anything is created.
    """
    planned = []
    for record in records:
        planned.append(
            Planned(
                record=record,
                kind=KIND_FOR[record.kind],
                project=project or record.project,
                tags=[identity_tag(namespace, record.source_id), *record.tags],
            )
        )
    return planned
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_import_mapping.py --cache-clear -q`
Expected: 7 passed.

- [ ] **Step 5: Prove the guards bite**

Map `SourceKind.SUMMARY` to `Kind.NOTE` - `test_a_summary_becomes_a_doc_not_a_handoff` must go red. Drop the identity tag from `plan` - `test_every_record_carries_its_identity_tag` must go red. Make `project or record.project` into `record.project` - the override test must go red. Clear `__pycache__` between variants. Restore.

- [ ] **Step 6: Commit**

```bash
make check
git add -A
git commit -m "Decide what an imported record becomes"
```

---

### Task 5: Writing, and the idempotency that makes re-import safe

**Files:**
- Modify: `src/remem/services/import_.py`
- Test: `tests/test_import_write.py` (new, **`db` marker**)

**Interfaces:**
- Consumes: `Planned`, `Report`, `plan`, `identity_tag` (Task 4); `Origin.IMPORTED` (Task 1).
- Produces: `services.import_.run(store, owner_id, records, *, namespace, project=None, dry_run=False) -> Report`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_import_write.py`:

```python
"""Importing twice must not duplicate. Needs a store."""

from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Kind, Origin, Query
from remem.importers.base import SourceKind, SourceRecord
from remem.services.import_ import run

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def _record(**kw) -> SourceRecord:
    base = dict(
        source_id="m1",
        kind=SourceKind.OBSERVATION,
        project="at-workspace",
        title="A title",
        summary="A hook",
        body="the original body",
        tags=("cmem-type:discovery",),
    )
    return SourceRecord(**{**base, **kw})


def _live(store, owner):
    return store.search(
        Query(tags=["cmem:m1"], origins=[Origin.IMPORTED], limit=50), owner.id
    )


def test_an_import_writes_an_entry_with_the_imported_origin(store, owner):
    report = run(store, owner.id, [_record()], namespace="cmem")

    assert report.created == 1
    [hit] = _live(store, owner)
    assert hit.entry.origin is Origin.IMPORTED
    assert hit.entry.kind is Kind.NOTE
    assert hit.entry.summary == "A hook"


def test_importing_the_same_record_twice_writes_nothing_the_second_time(store, owner):
    run(store, owner.id, [_record()], namespace="cmem")

    report = run(store, owner.id, [_record()], namespace="cmem")

    assert report.created == 0
    assert report.unchanged == 1
    assert len(_live(store, owner)) == 1


def test_a_changed_record_supersedes_rather_than_duplicating(store, owner):
    run(store, owner.id, [_record()], namespace="cmem")

    report = run(store, owner.id, [_record(body="an edited body")], namespace="cmem")

    assert report.updated == 1
    [hit] = _live(store, owner)
    assert hit.entry.body == "an edited body"


def test_the_replacement_keeps_the_identity_tag(store, owner):
    """Without this a third import duplicates: the tag is the only thing
    that answers 'have I seen this row before'."""
    run(store, owner.id, [_record()], namespace="cmem")
    run(store, owner.id, [_record(body="an edited body")], namespace="cmem")

    run(store, owner.id, [_record(body="an edited body")], namespace="cmem")

    assert len(_live(store, owner)) == 1


def test_a_dry_run_writes_nothing(store, owner):
    report = run(store, owner.id, [_record()], namespace="cmem", dry_run=True)

    assert report.dry_run is True
    assert report.created == 1, "a dry run still reports what it would do"
    assert _live(store, owner) == []


def test_a_dry_run_counts_by_kind_and_project(store, owner):
    records = [
        _record(),
        _record(source_id="m2", kind=SourceKind.SUMMARY, project="other"),
    ]

    report = run(store, owner.id, records, namespace="cmem", dry_run=True)

    assert report.by_project == {"at-workspace": 1, "other": 1}
    assert report.by_kind == {"observation": 1, "summary": 1}


def test_a_deleted_source_row_never_removes_an_entry(store, owner):
    """No orphan sweep. The source is being decommissioned - a row that
    stops existing there must not delete knowledge here."""
    run(store, owner.id, [_record()], namespace="cmem")

    run(store, owner.id, [], namespace="cmem")

    assert len(_live(store, owner)) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose up -d && uv run pytest tests/test_import_write.py --cache-clear -q`
Expected: FAIL - `ImportError: cannot import name 'run'`. If instead every test *skips*, Postgres is not reachable and the run proved nothing.

- [ ] **Step 3: Implement `run`**

Append to `src/remem/services/import_.py` (and extend the imports):

```python
from typing import Final
from uuid import UUID

from remem.domain import Entry, Origin, Query
from remem.services import write
from remem.store import Store

#: A source row is one entry, so a tag lookup should return one hit. The cap
#: is a guard against a namespace collision silently superseding the wrong
#: entry, not a real expectation.
MAX_PER_TAG: Final = 10


def run(
    store: Store,
    owner_id: UUID,
    records: list[SourceRecord],
    *,
    namespace: str,
    project: str | None = None,
    dry_run: bool = False,
) -> Report:
    """Import records, skipping what has not changed.

    Fail-loud, like `ingest` and `embed` and unlike every hook here: a person
    typed this and is watching it.
    """
    report = Report(dry_run=dry_run)
    for item in plan(records, namespace=namespace, project=project):
        kind_name = str(item.record.kind)
        report.by_kind[kind_name] = report.by_kind.get(kind_name, 0) + 1
        if item.project:
            report.by_project[item.project] = (
                report.by_project.get(item.project, 0) + 1
            )

        tag = identity_tag(namespace, item.record.source_id)
        existing = _existing(store, owner_id, tag)
        if existing is None:
            report.created += 1
            if not dry_run:
                write.remember(
                    store,
                    owner_id,
                    title=item.record.title,
                    body=item.record.body,
                    summary=item.record.summary,
                    kind=item.kind,
                    project=item.project,
                    tags=item.tags,
                    origin=Origin.IMPORTED,
                )
        elif existing.body != item.record.body:
            report.updated += 1
            if not dry_run:
                # supersede, not store.set_superseded: the replacement is
                # exactly what we have. Ingest's orphan sweep calls the store
                # directly only because a deleted heading has none. supersede
                # carries tags, kind, project and origin across, which is what
                # keeps the identity tag alive for the next run.
                write.supersede(
                    store,
                    owner_id,
                    existing.id,
                    title=item.record.title,
                    body=item.record.body,
                    summary=item.record.summary,
                )
        else:
            report.unchanged += 1
    return report


def _existing(store: Store, owner_id: UUID, tag: str) -> Entry | None:
    hits = store.search(
        Query(tags=[tag], origins=[Origin.IMPORTED], limit=MAX_PER_TAG),
        owner_id,
    )
    if not hits:
        return None
    return hits[0].entry
```

- [ ] **Step 4: Refuse a schema that predates migration 019**

The spec requires this and it is easy to skip: without `imported` in the
enum, `write.remember` raises a raw psycopg `InvalidTextRepresentation`
naming a Postgres type nobody has heard of. Add to `tests/test_import_write.py`:

```python
def test_a_schema_without_the_imported_origin_is_refused_by_name(conn, owner):
    """A new enum value cannot be USED in the transaction that added it, so
    a store one migration behind fails at the first write. The message must
    name `remem db up`, not a psycopg type error."""
    conn.execute("alter type entry_origin rename value 'imported' to 'imported_x'")
    store = PostgresStore(conn)

    with pytest.raises(SchemaTooOld, match="remem db up"):
        run(store, owner.id, [_record()], namespace="cmem")
```

Import `SchemaTooOld` from `remem.services.import_`, and add it there:

```python
class SchemaTooOld(Exception):
    """The database has no `imported` origin, so migration 019 has not been
    applied. Raised instead of letting psycopg surface an enum error naming a
    type the user has never heard of."""


def _require_origin(store: Store, owner_id: UUID) -> None:
    """Probed once per import, before anything is written. A partial import
    that dies on its first row is worse than one that never starts."""
    try:
        store.search(Query(origins=[Origin.IMPORTED], limit=1), owner_id)
    except Exception as exc:  # noqa: BLE001 - re-raised as a named error
        raise SchemaTooOld(
            "this database has no 'imported' origin, so migration 019 has not "
            "been applied. Run `remem db up`, then import again."
        ) from exc
```

Call `_require_origin(store, owner_id)` as the first line of `run()`, before
the loop - including on a dry run, since a dry run whose real counterpart
cannot possibly work is a misleading success.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_import_write.py --cache-clear -q`
Expected: 8 passed, **0 skipped**.

- [ ] **Step 6: Prove the guards bite**

Remove the `existing is None` branch so every record is written unconditionally - `test_importing_the_same_record_twice_writes_nothing_the_second_time` must go red. Replace `write.supersede` with a fresh `write.remember` - `test_the_replacement_keeps_the_identity_tag` must go red. Make `run` write during a dry run - `test_a_dry_run_writes_nothing` must go red. Clear `__pycache__` between variants. Restore.

- [ ] **Step 7: Commit**

```bash
make check
git add -A
git commit -m "Write imported records, and skip what has not changed"
```

---

### Task 6: `remem import claude-mem`, and the docs

The frontend, plus the CLAUDE.md section. Folded into one task because the docs describe exactly what this command does and a reviewer should see both together.

**Files:**
- Modify: `src/remem/cli.py`
- Modify: `CLAUDE.md`
- Test: `tests/test_import_cli.py` (new, **`db` marker**)

**Interfaces:**
- Consumes: `import_.run`, `import_.Report` (Task 5); `claude_mem.read`, `claude_mem.NAMESPACE`, `claude_mem.UnreadableSource` (Tasks 2-3).
- Produces: the `import` Typer sub-app with one command, `claude-mem`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_import_cli.py`. Follow the bootstrap in `tests/test_memory_cli.py` - these commands open their own session, so the schema must be committed before the CLI connects, and the DSN is threaded through `env=`:

```python
"""CLI tests for `remem import claude-mem`."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from remem.cli import app

pytestmark = pytest.mark.db

runner = CliRunner()

SCHEMA = """
create table projects (
  id text primary key, name text not null, slug text, root_path text,
  metadata text not null default '{}',
  created_at_epoch integer not null, updated_at_epoch integer not null
);
create table memory_items (
  id text primary key, project_id text not null, server_session_id text,
  legacy_observation_id integer, kind text not null, type text not null,
  title text, subtitle text, text text, narrative text,
  facts text not null default '[]', concepts text not null default '[]',
  files_read text not null default '[]',
  files_modified text not null default '[]',
  metadata text not null default '{}',
  created_at_epoch integer not null, updated_at_epoch integer not null
);
"""


@pytest.fixture
def source(tmp_path) -> Path:
    db = tmp_path / "claude-mem.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute(
        "insert into projects values (?,?,?,?,?,?,?)",
        ("p1", "at-workspace", "at-workspace", "/x", "{}", 1782832648, 1782832648),
    )
    conn.execute(
        "insert into memory_items values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "m1", "p1", "s1", None, "observation", "discovery",
            "A title", "A hook", None, "A narrative",
            json.dumps(["a fact"]), "[]", "[]", "[]", "{}",
            1782832648, 1782832648,
        ),
    )
    conn.commit()
    conn.close()
    return db


def test_a_dry_run_reports_counts_and_writes_nothing(env, source):
    result = runner.invoke(
        app, ["import", "claude-mem", str(source), "--dry-run"], env=env
    )

    assert result.exit_code == 0, result.output
    assert "at-workspace" in result.stdout
    assert "1" in result.stdout


def test_an_import_reports_what_it_created(env, source):
    result = runner.invoke(app, ["import", "claude-mem", str(source)], env=env)

    assert result.exit_code == 0, result.output
    assert "1 created" in result.stdout


def test_a_second_import_reports_nothing_changed(env, source):
    runner.invoke(app, ["import", "claude-mem", str(source)], env=env)

    result = runner.invoke(app, ["import", "claude-mem", str(source)], env=env)

    assert "1 unchanged" in result.stdout


def test_a_file_that_is_not_a_claude_mem_database_exits_nonzero(env, tmp_path):
    junk = tmp_path / "notes.txt"
    junk.write_text("not sqlite")

    result = runner.invoke(app, ["import", "claude-mem", str(junk)], env=env)

    assert result.exit_code == 1
    assert "notes.txt" in result.stderr


def test_a_missing_file_exits_nonzero(env, tmp_path):
    result = runner.invoke(
        app, ["import", "claude-mem", str(tmp_path / "nope.db")], env=env
    )

    assert result.exit_code == 1
```

Copy the `env` fixture verbatim from `tests/test_memory_cli.py` (it commits the schema, then hands the CLI a DSN and config path through the environment).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_import_cli.py --cache-clear -q`
Expected: FAIL with exit code 2 - "No such command 'import'".

- [ ] **Step 3: Add the command**

In `src/remem/cli.py`, beside the other sub-apps:

```python
import_app = typer.Typer(help="Import knowledge from another tool's store.")
app.add_typer(import_app, name="import")


@import_app.command("claude-mem")
def import_claude_mem(
    path: Annotated[str, typer.Argument(help="Path to claude-mem's sqlite file.")],
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
):
    """Load a claude-mem database in as entries with origin='imported'.

    Re-runnable: an entry whose `cmem:<id>` tag is already present and whose
    body is unchanged is skipped without a write. There is no orphan sweep -
    a row deleted from claude-mem never deletes anything here, because the
    source is being decommissioned and this is a migration, not a sync.
    """
    source = Path(path)
    try:
        records = claude_mem.read(source)
    except claude_mem.UnreadableSource as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    with _session() as s:
        report = import_.run(
            s.store,
            s.owner.id,
            records,
            namespace=claude_mem.NAMESPACE,
            project=project,
            dry_run=dry_run,
        )

    if dry_run:
        typer.echo("Dry run - nothing was written.")
    for name, count in sorted(report.by_project.items()):
        typer.echo(f"  {name}: {count}")
    for name, count in sorted(report.by_kind.items()):
        typer.echo(f"  {name}: {count}")
    typer.echo(
        f"{report.created} created, {report.updated} updated, "
        f"{report.unchanged} unchanged"
    )
    if report.created or report.updated:
        typer.echo("Run `remem embed` to give the new entries vectors.")
```

Add the imports at the top: `from remem.importers import claude_mem` and `from remem.services import import_`.

Note `claude_mem.read` is called **outside** `_session()`: a file that is not a database should be refused without opening a database connection at all.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_import_cli.py --cache-clear -q`
Expected: 5 passed, 0 skipped.

- [ ] **Step 5: Document it in CLAUDE.md**

Add a section after "### Ingested documents". Cover, in this repo's voice - reasons, not just behaviour:

- `remem import claude-mem <path>` reads a **local** sqlite file, because remem is installed on the machine that has the data; that choice is what removes the transport problem, and it is why there is no `--host` or remote mode.
- Identity is the `cmem:<id>` tag, the same role `src:`/`sec:` plays for ingest, which is what makes re-import safe.
- **No orphan sweep**, and why that is the deliberate opposite of ingest.
- Summaries are `kind=doc`, not `origin=handoff`, and what handoff's invariants would have done to fifteen historical summaries.
- Prompts group one entry per session, and why per-prompt entries were rejected.
- `Origin.IMPORTED` is in `DEFAULT_ORIGINS` and not in `INJECTED_ORIGINS`, with the standing warning that `DEFAULT_ORIGINS` must gain any future origin.
- Both schema shapes are read, detected via `sqlite_master`, because the source machine's version cannot be verified from here.
- No embedder is constructed; `remem embed` fills vectors afterwards.

- [ ] **Step 6: Prove the guards bite**

Move `claude_mem.read` inside the `with _session()` block - `test_a_file_that_is_not_a_claude_mem_database_exits_nonzero` should still pass, which tells you it does not pin the ordering; add an assertion or accept it knowingly. Make `--dry-run` fall through to a real write. `test_a_dry_run_reports_counts_and_writes_nothing` will NOT catch it - it only reads stdout, and Task 5's `test_a_dry_run_writes_nothing` covers the service, not the wiring. That gap is real: add a CLI-level assertion that after a dry run, `runner.invoke(app, ["search", "A title", "--json"], env=env)` returns an empty list. Watch it fail under the broken wiring, then restore. Clear `__pycache__` between variants.

- [ ] **Step 7: Full verification**

```bash
docker compose up -d
make check
```

Expected: `0 errors`, all tests pass, **skip count zero**. If `make check` reports skips, `db`-marked tests did not run and the idempotency guarantees are unproven.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "Add remem import claude-mem"
```

---

### Task 7: Prove it against the real export

The reference snapshot in the Obsidian vault is the legacy shape, in JSON rather than sqlite. This task turns it into a database and imports it, which is the only end-to-end evidence that the legacy reader matches reality rather than matching its own fixture.

**Files:**
- Create: nothing committed. Throwaway script in the scratchpad directory.

- [ ] **Step 1: Build a sqlite database from the real export**

The export lives at `~/Documents/Obsidian/AccuTrade/Claude Memory/claude-mem-export/`, as `_raw_observations.json` (52 rows), `_raw_session_summaries.json` (15), `_raw_user_prompts.json` (17). Write a throwaway script in the scratchpad that creates the three legacy tables and inserts every row, taking column names from the JSON keys.

- [ ] **Step 2: Dry-run the import against a scratch project**

```bash
remem import claude-mem "$SCRATCH/from-export.db" --project import-rehearsal --dry-run
```

Expected: 52 observations, 15 summaries, and prompts grouped into fewer than 17 records. Confirm the project line reads `import-rehearsal`.

- [ ] **Step 3: Import for real, into the scratch project**

Rehearse against a throwaway project name before any real one - this repo's rule for destructive or hard-to-undo automation. Then:

```bash
remem search "workspace topology" --limit 5
remem search "workspace topology" --limit 5 --json
```

Confirm imported entries appear in ordinary search results, and that `match` is present on each hit.

- [ ] **Step 4: Confirm the context block is unaffected**

```bash
remem hook context --agent claude-code < /dev/null
```

Expected: no imported entry in the block. `INJECTED_ORIGINS` excludes them; this is the check that the exclusion works end to end rather than only in a unit test.

- [ ] **Step 5: Re-run the import and confirm it is a no-op**

```bash
remem import claude-mem "$SCRATCH/from-export.db" --project import-rehearsal
```

Expected: `0 created, 0 updated, 84-ish unchanged`.

- [ ] **Step 6: Clean up the rehearsal**

The rehearsal wrote real rows into the real store under project
`import-rehearsal`. List them first, delete by id second, and never with an
operator-facing bulk command - per this repo's rule, a command built for
operators can delete another principal's rows, and a scratch cleanup is not
worth that risk.

```bash
remem search "" --project import-rehearsal --limit 100 --json | \
  python3 -c "import json,sys; [print(e['id']) for e in json.load(sys.stdin)]"
```

Then supersede or remove those ids individually. If no single-entry delete
exists, leave them and say so in the commit message rather than reaching for
a bulk command - a documented leftover is better than a destructive
improvisation.

- [ ] **Step 7: Record what the rehearsal proved**

If the real export disagrees with the legacy fixture in any column, fix `tests/test_claude_mem_reader_legacy.py` to match reality and re-run Task 3's guards. That disagreement is the single most valuable thing this task can find.

```bash
make check
git add -A
git commit -m "Fix the legacy fixture against the real export"   # only if anything changed
```
