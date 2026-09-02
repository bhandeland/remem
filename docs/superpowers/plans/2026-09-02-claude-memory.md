# Claude Code Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `~/.claude/projects/<cwd-slug>/memory/` a generated view of a designated remem collection, adopting anything Claude wrote there before regenerating, so a fact is written once and appears in both stores.

**Architecture:** A pure format module (`memory_file.py`) parses and renders memory files; a service (`services/memory.py`) owns every policy decision including a six-case sync driven by a `.remem-sync.json` watermark; a seventh probed adapter capability (`memory_dir(cwd)`) supplies the Claude-Code-specific path. `Entry` gains a nullable `summary` because a memory file carries three strings where `Entry` had two.

**Tech Stack:** Python 3.14, Typer, psycopg 3, Postgres 18, pytest.

**Spec:** `docs/superpowers/specs/2026-09-02-claude-memory-design.md`

## Global Constraints

- Python 3.14. `from __future__ import annotations` at the top of every new module.
- Strict layering, downward only: `cli.py` -> `services/memory.py` -> `store.py`. The CLI decides nothing; every policy branch lives in the service.
- In prose, comments and docs: spaced hyphens ` - `, never em dashes. The single exception is the generated `MEMORY.md` link/hook separator, which is `— ` because that format belongs to Claude Code.
- Comments explain *why*, at length, especially where a decision looks arbitrary.
- Migrations: add a new numbered `.sql` file, never edit an applied one. `migrate()` runs inside the caller's transaction.
- Timestamps use `clock_timestamp()`, not `now()`.
- Tests that need Postgres are marked `@pytest.mark.db`. Tests that do not MUST NOT be, so they run on CI.
- Fail-loud in this feature, unlike hooks: `remem memory` is a command a person typed.
- Never destructive on doubt: a file whose sha does not match its watermark is never deleted; a file changed on both sides is never overwritten.

### Deviation from the spec, deliberate

The spec says `Entry.summary` and the designation table land in "the same
migration". This plan uses **two** numbered migrations - `013_entry_summary.sql`
and `014_memory_settings.sql` - so Task 1 and Task 2 are each independently
testable and neither has to edit a migration the other already applied. That
is the repo's stated migration discipline winning over a convenience line in
the spec. Nothing else about the design changes.

---

### Task 1: `Entry.summary` end to end

A memory file carries three strings (index link text, frontmatter
`description:`, body) and `Entry` had two. Adding `summary` is what makes the
round-trip lossless; without it every export rewrites the description and the
watermark churns forever.

**Files:**
- Create: `src/remem/backends/postgres/migrations/013_entry_summary.sql`
- Modify: `src/remem/domain.py` (the `Entry` dataclass)
- Modify: `src/remem/backends/postgres/store.py` (`ENTRY_FIELDS`, `_row_to_entry`, `put_entry`)
- Modify: `src/remem/services/write.py` (`remember`, `supersede`)
- Test: `tests/test_entry_summary.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Entry.summary: str | None = None` (dataclass field, placed after `body`)
  - `write.remember(..., summary: str | None = None) -> Entry`
  - `write.supersede(store, owner_id, entry_id, *, title, body, summary: str | None = None) -> Entry` - when `summary` is `None` the old entry's summary is carried onto the replacement, exactly as `tags`, `kind`, `project`, `agent` and `origin` already are.

- [ ] **Step 1: Write the failing test**

Create `tests/test_entry_summary.py`:

```python
from __future__ import annotations

import pytest

from remem.services.write import remember, supersede


@pytest.mark.db
def test_summary_round_trips_through_the_store(store, owner):
    entry = remember(
        store, owner.id, title="T", body="B", summary="one line",
    )
    read_back = store.get_entry(entry.id, owner.id)
    assert read_back.summary == "one line"


@pytest.mark.db
def test_summary_defaults_to_none(store, owner):
    entry = remember(store, owner.id, title="T", body="B")
    assert store.get_entry(entry.id, owner.id).summary is None


@pytest.mark.db
def test_supersede_carries_the_summary_when_none_is_given(store, owner):
    old = remember(store, owner.id, title="T", body="B", summary="kept")
    new = supersede(store, owner.id, old.id, title="T2", body="B2")
    assert new.summary == "kept"


@pytest.mark.db
def test_supersede_replaces_the_summary_when_one_is_given(store, owner):
    old = remember(store, owner.id, title="T", body="B", summary="kept")
    new = supersede(
        store, owner.id, old.id, title="T2", body="B2", summary="replaced",
    )
    assert new.summary == "replaced"
```

Check `tests/conftest.py` for the exact names of the store and owner fixtures
other `db`-marked tests use (for example `tests/test_ingest.py`) and match them
rather than the placeholder names above if they differ.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_entry_summary.py -v`
Expected: FAIL - `TypeError: remember() got an unexpected keyword argument 'summary'`

- [ ] **Step 3: Write the migration**

Create `src/remem/backends/postgres/migrations/013_entry_summary.sql`:

```sql
-- A memory file carries three strings where an entry had two: the MEMORY.md
-- link text (title), the frontmatter description, and the body. Deriving the
-- description from the body was rejected because it would be rewritten on
-- every export, so the file's checksum would never match its watermark and
-- the sync would report a change on a file nobody touched.
--
-- Nullable and unread by anything else today. `entries.search` is a generated
-- column over title/body/tags and is deliberately NOT extended to cover
-- summary: the summary is a restatement of the body, so indexing it would
-- weight the same words twice.
alter table entries add column summary text;
```

- [ ] **Step 4: Add the field to the domain**

In `src/remem/domain.py`, in the `Entry` dataclass, immediately after `body`:

```python
    #: The one-line description a Claude Code memory file carries in its
    #: frontmatter. Nullable because every other origin has no such thing.
    summary: str | None = None
```

Note `Entry` uses `@dataclass(slots=True)` with defaulted fields after
`owner_id`; `summary` has a default, so place it among the defaulted fields
(after `body` will fail if `body` precedes non-defaulted fields - if it does,
put `summary` directly after `project` instead and adjust nothing else).

- [ ] **Step 5: Thread it through the Postgres store**

In `src/remem/backends/postgres/store.py`:

```python
ENTRY_FIELDS = [
    "id", "kind", "title", "body", "summary", "project", "scope", "owner_id",
    "tags", "links", "agent", "session_id", "origin", "superseded_by",
    "created_at", "updated_at",
]
```

In `_row_to_entry`, after `body=row["body"],`:

```python
        summary=row["summary"],
```

In `put_entry`, add `summary` to the insert column list, to the `values`
placeholders as `%(summary)s`, to the `on conflict do update set` clause as
`summary = excluded.summary`, and to the parameter dict as
`"summary": entry.summary,`.

Leave the `text_changed` probe above it alone: it selects `title, body` to
decide whether a vector is stale, and a summary change does not invalidate a
vector because the summary is not embedded.

- [ ] **Step 6: Thread it through the write service**

In `src/remem/services/write.py`, add `summary: str | None = None` to
`remember`'s keyword-only parameters and pass `summary=summary` into the
`Entry(...)` construction.

In `supersede`, add the parameter and carry the old value when it is absent:

```python
def supersede(
    store: Store,
    owner_id: UUID,
    entry_id: UUID,
    *,
    title: str,
    body: str,
    summary: str | None = None,
) -> Entry:
    """Replace knowledge that stopped being true. The old entry is kept."""
    old = _require(store, owner_id, entry_id)
    replacement = remember(
        store,
        owner_id,
        title=title,
        body=body,
        # Carried, not defaulted to None: supersede's contract is that the
        # replacement inherits everything the caller did not restate, which
        # is what makes tag-based identity survive an edit. A summary silently
        # dropped on every supersede would empty the frontmatter description
        # of any memory file that was ever edited.
        summary=old.summary if summary is None else summary,
        kind=old.kind,
        project=old.project,
        tags=list(old.tags),
        agent=old.agent,
        origin=old.origin,
    )
```

Leave the rest of the function body unchanged.

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_entry_summary.py -v`
Expected: 4 passed. If they SKIP, Postgres is not running - start it with
`docker compose up -d` and re-run. A skip is not a pass.

- [ ] **Step 8: Run the full suite**

Run: `uv run pytest`
Expected: the previous total plus 4, 0 failures. Note the skip count; it should
be 0 with Postgres up.

- [ ] **Step 9: Commit**

```bash
git add src/remem/domain.py src/remem/store.py src/remem/backends/postgres/ src/remem/services/write.py tests/test_entry_summary.py
git commit -m "feat: Entry.summary, carried through supersede

A Claude Code memory file carries three strings where Entry had two.
Deriving the description from the body would rewrite it on every export
and churn the sync watermark forever."
```

---

### Task 2: The designation gate

Per-project opt-in, holding a value rather than a boolean: which collection is
exported. An undesignated project generates nothing.

**Files:**
- Create: `src/remem/backends/postgres/migrations/014_memory_settings.sql`
- Create: `src/remem/services/memory.py`
- Modify: `src/remem/store.py` (Protocol)
- Modify: `src/remem/backends/postgres/store.py`
- Test: `tests/test_memory_designation.py`

**Interfaces:**
- Consumes: `kb.get(store, owner_id, slug) -> Collection`, which raises `kb.CollectionNotFound(slug)`.
- Produces:
  - `Store.set_memory_collection(owner_id: UUID, project: str, slug: str | None) -> None`
  - `Store.memory_collection(owner_id: UUID, project: str) -> str | None`
  - `services.memory.designate(store, owner_id, project, slug: str | None) -> None`
  - `services.memory.designation(store, owner_id, project) -> str | None`
  - `services.memory.NotDesignated(Exception)`

- [ ] **Step 1: Write the failing test**

Create `tests/test_memory_designation.py`:

```python
from __future__ import annotations

import pytest

from remem.services import kb, memory


@pytest.mark.db
def test_a_project_starts_undesignated(store, owner):
    assert memory.designation(store, owner.id, "proj") is None


@pytest.mark.db
def test_designate_records_the_collection(store, owner):
    kb.create(store, owner.id, slug="proj-memory", name="Memory",
              project="proj")
    memory.designate(store, owner.id, "proj", "proj-memory")
    assert memory.designation(store, owner.id, "proj") == "proj-memory"


@pytest.mark.db
def test_designate_refuses_a_collection_that_does_not_exist(store, owner):
    with pytest.raises(kb.CollectionNotFound):
        memory.designate(store, owner.id, "proj", "nope")
    assert memory.designation(store, owner.id, "proj") is None


@pytest.mark.db
def test_designate_none_clears_it(store, owner):
    kb.create(store, owner.id, slug="proj-memory", name="Memory",
              project="proj")
    memory.designate(store, owner.id, "proj", "proj-memory")
    memory.designate(store, owner.id, "proj", None)
    assert memory.designation(store, owner.id, "proj") is None


@pytest.mark.db
def test_designation_is_per_project(store, owner):
    kb.create(store, owner.id, slug="a-memory", name="A", project="a")
    memory.designate(store, owner.id, "a", "a-memory")
    assert memory.designation(store, owner.id, "b") is None
```

Read `src/remem/services/kb.py`'s `create` signature before writing these and
match it exactly; the call above is the expected shape, not a guess you should
keep if it differs.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_memory_designation.py -v`
Expected: FAIL - `ImportError: cannot import name 'memory' from 'remem.services'`

- [ ] **Step 3: Write the migration**

Create `src/remem/backends/postgres/migrations/014_memory_settings.sql`:

```sql
-- Which collection is exported to Claude Code's memory directory, per
-- project. Shaped after record_settings - a per-project opt-in checked in
-- the service - but holding a value rather than a boolean, because the
-- question here is "which collection" and not "on or off". No row means
-- not designated, which means the sync writes nothing at all.
--
-- No foreign key to collections: a collection deleted out from under a
-- designation should leave the designation visibly dangling for the sync to
-- report, not cascade into silently un-designating a project.
create table memory_settings (
  owner_id uuid not null references principals(id),
  project text not null,
  collection_slug text not null,
  updated_at timestamptz not null default clock_timestamp(),
  primary key (owner_id, project)
);
```

- [ ] **Step 4: Add the two store methods**

In `src/remem/store.py`, after the `# recording` block:

```python
    # memory export
    def set_memory_collection(
        self, owner_id: UUID, project: str, slug: str | None
    ) -> None: ...
    def memory_collection(self, owner_id: UUID, project: str) -> str | None: ...
```

In `src/remem/backends/postgres/store.py`, beside `set_record_enabled`:

```python
    def set_memory_collection(
        self, owner_id: UUID, project: str, slug: str | None
    ) -> None:
        with self._cur() as cur:
            if slug is None:
                cur.execute(
                    "delete from memory_settings "
                    "where owner_id = %s and project = %s",
                    (owner_id, project),
                )
                return
            cur.execute(
                """
                insert into memory_settings (owner_id, project, collection_slug)
                values (%s, %s, %s)
                on conflict (owner_id, project)
                  do update set collection_slug = excluded.collection_slug,
                                updated_at = clock_timestamp()
                """,
                (owner_id, project, slug),
            )

    def memory_collection(self, owner_id: UUID, project: str) -> str | None:
        with self._cur() as cur:
            cur.execute(
                "select collection_slug from memory_settings "
                "where owner_id = %s and project = %s",
                (owner_id, project),
            )
            row = cur.fetchone()
        return row["collection_slug"] if row else None
```

- [ ] **Step 5: Write the service**

Create `src/remem/services/memory.py`:

```python
"""Claude Code's file-based memory directory, as a generated view of a
designated collection.

Every policy decision lives here: which collection is exported, what a
conflict is, and which of the six cases a given file falls into. The CLI
resolves a working directory and a project and prints counts.

The directory has a second writer that cannot be told to stop - Claude Code
writes memories there unprompted, mid-session - so the sync adopts before it
regenerates. See docs/superpowers/specs/2026-09-02-claude-memory-design.md.
"""

from __future__ import annotations

from uuid import UUID

from remem.services import kb
from remem.store import Store


class NotDesignated(Exception):
    """This project has no memory collection, so nothing is exported.

    Not an error state to repair - it is the default, and the opt-in gate.
    Raised only by callers that were asked to sync a specific project.
    """


def designate(
    store: Store, owner_id: UUID, project: str, slug: str | None
) -> None:
    """Point a project's memory export at a collection, or clear it.

    The collection must already exist. Creating one here would give it an
    empty CollectionQuery, and an empty query matches nothing forever - so
    the friendly version of this command would silently guarantee that no
    memory is ever exported. `kb create` says so through kb.advisories();
    this does not get to bypass it.
    """
    if slug is not None:
        kb.get(store, owner_id, slug)  # raises CollectionNotFound
    store.set_memory_collection(owner_id, project, slug)


def designation(store: Store, owner_id: UUID, project: str) -> str | None:
    return store.memory_collection(owner_id, project)
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_memory_designation.py -v`
Expected: 5 passed.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest`
Expected: previous total plus 5, 0 failures, 0 skips.

- [ ] **Step 8: Commit**

```bash
git add src/remem/store.py src/remem/backends/postgres/ src/remem/services/memory.py tests/test_memory_designation.py
git commit -m "feat: per-project memory collection designation

Shaped after record_settings but holding a value, not a boolean. An
undesignated project generates nothing, which is the whole safety story
and what keeps MEMORY.md from double-loading against the SessionStart
block."
```

---

### Task 3: The pure format module

Parse and render a memory file and the `MEMORY.md` index. No I/O, no store,
so its tests run on CI.

**Files:**
- Create: `src/remem/memory_file.py`
- Create: `tests/fixtures/memory/` (corpus copied from real memory files)
- Test: `tests/test_memory_file.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `memory_file.MemoryFile` - frozen dataclass with fields `name: str`, `title: str`, `description: str`, `type: str | None`, `body: str`
  - `memory_file.parse(text: str, *, name: str, title: str) -> MemoryFile`
  - `memory_file.render(mf: MemoryFile) -> str`
  - `memory_file.render_index(files: list[MemoryFile]) -> str`
  - `memory_file.body_sha(body: str) -> str`
  - `memory_file.title_from_name(name: str) -> str`
  - `memory_file.parse_index(text: str) -> dict[str, str]` - filename stem to link text
  - `memory_file.MalformedMemoryFile(Exception)`

- [ ] **Step 1: Build the fixture corpus**

```bash
mkdir -p tests/fixtures/memory
cp ~/.claude/projects/-Users-brandon-llmworkspace-remem/memory/*.md tests/fixtures/memory/
ls tests/fixtures/memory/
```

Expected: `MEMORY.md` plus 5 fact files. These are the real thing; do not
hand-edit them. They are what makes the byte-for-byte test meaningful.

- [ ] **Step 2: Write the failing test**

Create `tests/test_memory_file.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from remem import memory_file

FIXTURES = Path(__file__).parent / "fixtures" / "memory"


def _fact_files() -> list[Path]:
    return sorted(p for p in FIXTURES.glob("*.md") if p.name != "MEMORY.md")


def test_the_fixture_corpus_is_actually_there():
    # A glob that silently matched nothing would make every test below pass
    # vacuously, which is the failure mode this whole file exists to avoid.
    assert len(_fact_files()) >= 5


@pytest.mark.parametrize("path", _fact_files(), ids=lambda p: p.name)
def test_render_of_parse_is_byte_for_byte_identical(path):
    text = path.read_text()
    index = memory_file.parse_index((FIXTURES / "MEMORY.md").read_text())
    parsed = memory_file.parse(
        text, name=path.stem, title=index.get(path.name, ""),
    )
    assert memory_file.render(parsed) == text


def test_parse_reads_every_field():
    text = (FIXTURES / "cursor-runs-claude-code-hooks.md").read_text()
    mf = memory_file.parse(
        text, name="cursor-runs-claude-code-hooks", title="Cursor runs hooks",
    )
    assert mf.name == "cursor-runs-claude-code-hooks"
    assert mf.title == "Cursor runs hooks"
    assert mf.type == "project"
    assert mf.description.startswith("Cursor 3.x loads")
    assert "Cursor 3.x reads" in mf.body


def test_parse_rejects_a_file_with_no_frontmatter():
    with pytest.raises(memory_file.MalformedMemoryFile):
        memory_file.parse("just a body", name="x", title="X")


def test_parse_index_maps_filename_to_link_text():
    index = memory_file.parse_index((FIXTURES / "MEMORY.md").read_text())
    assert index["cursor-runs-claude-code-hooks.md"] == (
        "Cursor runs Claude Code's hooks"
    )


def test_render_index_is_sorted_by_name_and_uses_the_em_dash():
    files = [
        memory_file.MemoryFile(
            name="b", title="B", description="second", type=None, body="x",
        ),
        memory_file.MemoryFile(
            name="a", title="A", description="first", type=None, body="x",
        ),
    ]
    assert memory_file.render_index(files) == (
        "- [A](a.md) — first\n"
        "- [B](b.md) — second\n"
    )


def test_title_from_name_is_mechanical():
    assert memory_file.title_from_name("cursor-runs-claude-code-hooks") == (
        "Cursor runs claude code hooks"
    )


def test_body_sha_ignores_nothing_and_is_stable():
    assert memory_file.body_sha("a") == memory_file.body_sha("a")
    assert memory_file.body_sha("a") != memory_file.body_sha("a\n")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_memory_file.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'remem.memory_file'`

- [ ] **Step 4: Write the module**

Create `src/remem/memory_file.py`:

```python
"""Parsing and rendering Claude Code memory files. Pure - no I/O, no store.

Kept out of services/ for the same reason markdown.py and session_size.py
are: a format bug should be caught on CI, and a module that touches no
database has tests that carry no `db` marker and therefore run there.

The round-trip is load-bearing rather than cosmetic. `render(parse(f))` must
equal `f` byte for byte, because the sync decides "nothing changed, write
nothing" by comparing checksums - a renderer that normalised whitespace
would report a change on every file, every run, forever.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

#: The link/hook separator in MEMORY.md. An em dash, against this repo's
#: spaced-hyphen convention, because this line's format belongs to Claude
#: Code and matching the corpus already on disk matters more than matching
#: remem's prose style. The only place in the repo where that is true.
INDEX_SEPARATOR = " — "

_INDEX_LINE = re.compile(r"^- \[(?P<title>.+?)\]\((?P<file>[^)]+)\)")


class MalformedMemoryFile(Exception):
    """A file that does not have the frontmatter a memory file must have."""


@dataclass(slots=True, frozen=True)
class MemoryFile:
    #: The frontmatter `name:` slug. The file's identity, and what
    #: `[[wiki-links]]` resolve against - not the filename, though the
    #: filename is derived from it on export.
    name: str
    #: The MEMORY.md link text. Lives in the index, not in this file, which
    #: is why parse() takes it as an argument.
    title: str
    #: The frontmatter `description:`.
    description: str
    type: str | None
    body: str


def body_sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def title_from_name(name: str) -> str:
    """A placeholder title for a stray file the index does not list yet.

    Deliberately mechanical and a little ugly, so the next MEMORY.md line
    written for it reads as an improvement rather than a conflict.
    """
    words = name.replace("-", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else name


def parse_index(text: str) -> dict[str, str]:
    """MEMORY.md filename -> link text. Lines it cannot read are skipped.

    Skipping rather than raising: the index is the file a human is most
    likely to have hand-edited, and one malformed line must not cost the
    titles of every other memory.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _INDEX_LINE.match(line.strip())
        if m:
            out[m.group("file")] = m.group("title")
    return out


def parse(text: str, *, name: str, title: str) -> MemoryFile:
    """One file plus its index title. Never a file in isolation.

    `name` is passed in rather than trusted from the frontmatter so the
    caller can key on the filename when the two disagree; when frontmatter
    carries a name it wins, since that is the identity `[[links]]` use.
    """
    if not text.startswith("---\n"):
        raise MalformedMemoryFile("no frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise MalformedMemoryFile("unterminated frontmatter")
    head = text[4:end + 1]
    body = text[end + len("\n---\n"):]

    fields: dict[str, str] = {}
    in_metadata = False
    for line in head.splitlines():
        if not line.strip():
            continue
        if line.startswith("metadata:"):
            in_metadata = True
            continue
        key, _, value = line.strip().partition(":")
        value = value.strip()
        if in_metadata and line.startswith(" "):
            fields[f"metadata.{key.strip()}"] = value
        else:
            in_metadata = False
            fields[key.strip()] = value

    return MemoryFile(
        name=fields.get("name") or name,
        title=title or title_from_name(fields.get("name") or name),
        description=fields.get("description", ""),
        type=fields.get("metadata.type"),
        body=body,
    )


def render(mf: MemoryFile) -> str:
    """The inverse of parse(), byte for byte. See the module docstring."""
    lines = [
        "---",
        f"name: {mf.name}",
        f"description: {mf.description}",
    ]
    if mf.type is not None:
        lines += ["metadata:", f"  type: {mf.type}"]
    lines += ["---", ""]
    return "\n".join(lines) + "\n" + mf.body


def render_index(files: list[MemoryFile]) -> str:
    """Sorted by name. Any deterministic order would do; the requirement is
    only that it not depend on iteration order, because an index that
    reshuffles itself makes every sync look like a change."""
    return "".join(
        f"- [{mf.title}]({mf.name}.md){INDEX_SEPARATOR}{mf.description}\n"
        for mf in sorted(files, key=lambda m: m.name)
    )
```

- [ ] **Step 5: Run the tests and fix the renderer against the real corpus**

Run: `uv run pytest tests/test_memory_file.py -v`

Expected: the byte-for-byte test may FAIL on the first attempt. That is the
point of it. Read the diff, and adjust `render` - not the fixtures - until
every real file round-trips exactly. The likely culprits are the blank line
after the closing `---` and the trailing newline; do not "fix" them by
stripping or normalising in `parse`, because the checksum is taken over what
`render` writes.

Expected once correct: all tests pass, none skipped.

- [ ] **Step 6: Confirm these tests run without a database**

Run: `uv run pytest tests/test_memory_file.py -m 'not db' -v`
Expected: the same count passes. If anything is deselected, a `db` marker
crept in; remove it. This module must be covered on CI.

- [ ] **Step 7: Commit**

```bash
git add src/remem/memory_file.py tests/test_memory_file.py tests/fixtures/memory/
git commit -m "feat: pure parse/render for Claude Code memory files

render(parse(f)) is byte-for-byte over a corpus copied from the real
memory directory. That property is load-bearing: the sync decides
'nothing changed' by checksum, so a renderer that normalised whitespace
would report a change on every file forever."
```

---

### Task 4: The watermark and the case classifier

`.remem-sync.json` is what makes "which side moved" answerable. The classifier
is a pure function, so the six cases are tested without a database.

**Files:**
- Modify: `src/remem/services/memory.py`
- Test: `tests/test_memory_cases.py`

**Interfaces:**
- Consumes: `memory_file.body_sha`.
- Produces:
  - `memory.Case` - `StrEnum` with members `ADOPT_NEW`, `HEAL`, `ADOPT_EDIT`, `REGENERATE`, `CONFLICT`, `DELETE`, `UNCHANGED`
  - `memory.Watermark` - frozen dataclass, fields `entry_id: str`, `body_sha: str`, `exported_at: str`
  - `memory.classify(*, file_sha: str | None, entry_sha: str | None, mark: Watermark | None) -> Case`
  - `memory.load_watermarks(directory: Path) -> dict[str, Watermark]`
  - `memory.save_watermarks(directory: Path, marks: dict[str, Watermark]) -> None`
  - `memory.WATERMARK_NAME = ".remem-sync.json"`

- [ ] **Step 1: Write the failing test**

Create `tests/test_memory_cases.py`:

```python
from __future__ import annotations

from remem.services import memory
from remem.services.memory import Case

MARK = memory.Watermark(entry_id="e", body_sha="A", exported_at="t")


def test_a_stray_file_is_adopted_as_new():
    assert memory.classify(file_sha="A", entry_sha=None, mark=None) == (
        Case.ADOPT_NEW
    )


def test_file_and_entry_with_no_watermark_and_equal_bodies_heals():
    assert memory.classify(file_sha="A", entry_sha="A", mark=None) == Case.HEAL


def test_file_and_entry_with_no_watermark_and_different_bodies_conflicts():
    assert memory.classify(file_sha="A", entry_sha="B", mark=None) == (
        Case.CONFLICT
    )


def test_file_moved_alone_is_adopted_as_an_edit():
    assert memory.classify(file_sha="B", entry_sha="A", mark=MARK) == (
        Case.ADOPT_EDIT
    )


def test_entry_moved_alone_regenerates_the_file():
    assert memory.classify(file_sha="A", entry_sha="B", mark=MARK) == (
        Case.REGENERATE
    )


def test_both_moved_is_a_conflict():
    assert memory.classify(file_sha="B", entry_sha="C", mark=MARK) == (
        Case.CONFLICT
    )


def test_both_moved_to_the_same_content_is_not_a_conflict():
    # Claude and remem independently arriving at the same text is agreement,
    # not a conflict, and asking the user to resolve it would be noise.
    assert memory.classify(file_sha="B", entry_sha="B", mark=MARK) == Case.HEAL


def test_neither_moved_is_unchanged():
    assert memory.classify(file_sha="A", entry_sha="A", mark=MARK) == (
        Case.UNCHANGED
    )


def test_an_entry_with_no_file_is_regenerated():
    assert memory.classify(file_sha=None, entry_sha="A", mark=None) == (
        Case.REGENERATE
    )


def test_an_entry_gone_from_the_collection_deletes_its_file():
    assert memory.classify(file_sha="A", entry_sha=None, mark=MARK) == (
        Case.DELETE
    )


def test_a_file_that_moved_since_export_is_never_deleted():
    # The gate: remem removes only what it wrote and knows to be untouched.
    assert memory.classify(file_sha="B", entry_sha=None, mark=MARK) == (
        Case.ADOPT_EDIT
    )


def test_watermarks_round_trip(tmp_path):
    marks = {"a": MARK}
    memory.save_watermarks(tmp_path, marks)
    assert memory.load_watermarks(tmp_path) == marks


def test_a_missing_watermark_file_is_an_empty_mapping(tmp_path):
    assert memory.load_watermarks(tmp_path) == {}


def test_a_corrupt_watermark_file_is_an_empty_mapping(tmp_path):
    (tmp_path / memory.WATERMARK_NAME).write_text("{not json")
    assert memory.load_watermarks(tmp_path) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_memory_cases.py -v`
Expected: FAIL - `AttributeError: module 'remem.services.memory' has no attribute 'Watermark'`

- [ ] **Step 3: Implement**

Add to `src/remem/services/memory.py` (imports first: `json`, `StrEnum` from
`enum`, `dataclass` from `dataclasses`, `Path` from `pathlib`):

```python
#: Dot-prefixed so Claude Code does not index it as a memory.
WATERMARK_NAME = ".remem-sync.json"


class Case(StrEnum):
    ADOPT_NEW = "adopt_new"
    HEAL = "heal"
    ADOPT_EDIT = "adopt_edit"
    REGENERATE = "regenerate"
    CONFLICT = "conflict"
    DELETE = "delete"
    UNCHANGED = "unchanged"


@dataclass(slots=True, frozen=True)
class Watermark:
    entry_id: str
    body_sha: str
    #: Never compared. Shown by `remem memory status` so a stale directory is
    #: visible; comparing it would reintroduce the clocks this whole scheme
    #: exists to avoid.
    exported_at: str


def classify(
    *,
    file_sha: str | None,
    entry_sha: str | None,
    mark: Watermark | None,
) -> Case:
    """Which of the six cases this name falls into.

    Pure, and separated from the sync deliberately: this is the part with all
    the combinations, and a pure function means every one of them is tested
    on CI without a database.

    Body comparison alone cannot answer "which side moved" - it says the two
    differ, not who changed. The watermark is what makes it answerable.
    """
    if file_sha is None and entry_sha is None:
        return Case.UNCHANGED  # nothing on either side; nothing to do
    if file_sha is None:
        return Case.REGENERATE  # entry with no file - write it
    if entry_sha is None:
        # No entry. Delete only what we wrote and know to be untouched;
        # anything else is an edit worth keeping.
        if mark is not None and mark.body_sha == file_sha:
            return Case.DELETE
        return Case.ADOPT_EDIT if mark is not None else Case.ADOPT_NEW
    if file_sha == entry_sha:
        # Agreement. Either it never moved, or both sides moved to the same
        # text - which is agreement too, not a conflict worth a user's time.
        return Case.UNCHANGED if mark is not None and mark.body_sha == file_sha else Case.HEAL
    if mark is None:
        # They differ and there is no watermark, so nothing says which moved.
        return Case.CONFLICT
    file_moved = mark.body_sha != file_sha
    entry_moved = mark.body_sha != entry_sha
    if file_moved and entry_moved:
        return Case.CONFLICT
    return Case.ADOPT_EDIT if file_moved else Case.REGENERATE


def load_watermarks(directory: Path) -> dict[str, Watermark]:
    """Missing or unreadable is an empty mapping, not an error.

    Degrading here is safe because of how classify() treats a missing mark:
    a file that still matches its entry heals, and one that differs becomes a
    conflict a human resolves. Losing this file costs attention, never data.
    """
    path = directory / WATERMARK_NAME
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Watermark] = {}
    for name, row in raw.items():
        try:
            out[name] = Watermark(
                entry_id=row["entry_id"],
                body_sha=row["body_sha"],
                exported_at=row["exported_at"],
            )
        except (TypeError, KeyError):
            continue
    return out


def save_watermarks(directory: Path, marks: dict[str, Watermark]) -> None:
    path = directory / WATERMARK_NAME
    payload = {
        name: {
            "entry_id": m.entry_id,
            "body_sha": m.body_sha,
            "exported_at": m.exported_at,
        }
        for name, m in sorted(marks.items())
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_memory_cases.py -v`
Expected: 14 passed. Count them by name against the test file before moving
on - the file above specifies 14 tests, and a number lower than that means a
test was dropped in transcription.

- [ ] **Step 5: Confirm no database is needed**

Run: `uv run pytest tests/test_memory_cases.py -m 'not db' -v`
Expected: the same 14 pass.

- [ ] **Step 6: Commit**

```bash
git add src/remem/services/memory.py tests/test_memory_cases.py
git commit -m "feat: sync watermark and the six-case classifier

classify() is pure so every combination is covered on CI. The watermark
is the only thing that answers 'which side moved' - body comparison says
the two differ, not who changed."
```

---

### Task 5: The `memory_dir` adapter capability

The seventh optional capability. The path is keyed by Claude Code's slug of
the absolute working directory, which is not derivable from remem's project
name - and differs in a worktree, which is the ordinary case in this repo.

**Files:**
- Create: `src/remem/agents/claude_code/memory.py`
- Modify: `src/remem/agents/claude_code/adapter.py`
- Modify: `src/remem/agents/base.py` (the optional-capabilities comment block)
- Modify: `CLAUDE.md` (the count of six)
- Test: `tests/test_memory_capability.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `agents.claude_code.memory.slug_for(cwd: Path) -> str`
  - `agents.claude_code.memory.memory_dir(cwd: Path, env: Mapping[str, str] | None = None) -> Path`
  - `ClaudeCodeAdapter.memory_dir(self, cwd: Path, env: Mapping[str, str] | None = None) -> Path | None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_memory_capability.py`:

```python
from __future__ import annotations

from pathlib import Path

from remem.agents.claude_code import memory as cc_memory
from remem.agents.registry import get


def test_the_slug_is_the_absolute_path_with_separators_replaced():
    assert cc_memory.slug_for(Path("/Users/brandon/llmworkspace/remem")) == (
        "-Users-brandon-llmworkspace-remem"
    )


def test_a_worktree_gets_its_own_slug():
    # Deliberately NOT the project name: remem's project resolves through
    # --git-common-dir so a worktree shares it, while Claude Code keys the
    # directory on the working directory alone.
    a = cc_memory.slug_for(Path("/Users/b/work/remem"))
    b = cc_memory.slug_for(Path("/Users/b/work/remem-feature"))
    assert a != b


def test_memory_dir_sits_under_the_claude_home():
    got = cc_memory.memory_dir(
        Path("/w/proj"), env={"CLAUDE_CONFIG_DIR": "/cfg"},
    )
    assert got == Path("/cfg/projects/-w-proj/memory")


def test_memory_dir_defaults_to_dot_claude_in_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    got = cc_memory.memory_dir(Path("/w/proj"), env={})
    assert got == tmp_path / ".claude" / "projects" / "-w-proj" / "memory"


def test_the_adapter_exposes_the_capability():
    adapter = get("claude-code")
    assert getattr(adapter, "memory_dir", None) is not None
    assert adapter.memory_dir(
        Path("/w/proj"), env={"CLAUDE_CONFIG_DIR": "/cfg"},
    ) == Path("/cfg/projects/-w-proj/memory")


def test_another_adapter_does_not_pretend_to_have_it():
    # A probed capability: opencode has no such directory, and reporting one
    # would send the sync to a path nothing reads.
    assert getattr(get("opencode"), "memory_dir", None) is None
```

Read `src/remem/agents/registry.py` for the exact accessor name before
writing this - if it is not `get`, use whatever `tests/test_doctor.py` or
`tests/test_config.py` already use to fetch an adapter.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_memory_capability.py -v`
Expected: FAIL - `ImportError: cannot import name 'memory' from 'remem.agents.claude_code'`

- [ ] **Step 3: Write the module**

Create `src/remem/agents/claude_code/memory.py`:

```python
"""Where Claude Code keeps its file-based memory.

A fact about Claude Code, so it lives on the adapter rather than in
services/ - the same reason env_vars.py does. A second harness with a
memory directory would ship its own.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def slug_for(cwd: Path) -> str:
    """Claude Code's slug for a working directory: separators to hyphens.

    Note this is the *working directory*, not remem's project. They are not
    derivable from one another: remem resolves a project through
    --git-common-dir so a worktree shares the parent repository's name,
    while Claude Code keys this directory on the path alone. remem's own
    development happens in a worktree, so the two differ here routinely.
    """
    return str(Path(cwd).resolve()).replace(os.sep, "-")


def memory_dir(cwd: Path, env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    home = env.get("CLAUDE_CONFIG_DIR")
    root = Path(home) if home else Path(env.get("HOME", "~")).expanduser() / ".claude"
    return root / "projects" / slug_for(cwd) / "memory"
```

- [ ] **Step 4: Expose it on the adapter**

In `src/remem/agents/claude_code/adapter.py`, import the module and add the
method to the adapter class, following the file's existing import style:

```python
    def memory_dir(
        self, cwd: Path, env: Mapping[str, str] | None = None
    ) -> Path | None:
        return cc_memory.memory_dir(cwd, env)
```

- [ ] **Step 5: Document the seventh capability**

In `src/remem/agents/base.py`, inside the optional-capabilities comment
block, after the `hook_state` paragraph:

```python
    #     def memory_dir(
    #         self, cwd: Path, env: Mapping[str, str] | None = None
    #     ) -> Path | None: ...
    #
    # Where this harness keeps a file-based memory directory that remem can
    # own, given a working directory. Probed by services/memory.py; an
    # adapter without it simply has no such directory, which is the default
    # and not a degradation - opencode and Cursor have none. Note the
    # argument is a path, not a project: Claude Code keys its directory on
    # the working directory, which a worktree changes and remem's project
    # name does not. Same degradation contract as the rest: a capability
    # that raises warns and continues.
```

In `CLAUDE.md`, in the cursor-adapter section, change "**Injection is one of
six optional adapter capabilities**" to "**seven**", and extend the
parenthetical list "(`event()`, `env_settings()`, `settings_path()`,
`verify()` and `hook_state()`)" to include `memory_dir()`. CLAUDE.md says to
keep the count there and in `base.py` in step; this is the same commit.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_memory_capability.py -v`
Expected: 6 passed.

- [ ] **Step 7: Verify the count really is in step**

Run: `grep -c 'def [a-z_]*(' src/remem/agents/base.py` is not the check.
Instead read the comment block and count the signatures listed in it by name,
out loud, in your report: they should be `env_settings`, `settings_path`,
`event`, `inject`, `verify`, `hook_state`, `memory_dir` - **seven**. Confirm
`CLAUDE.md` now says seven. State both numbers in your task report.

- [ ] **Step 8: Commit**

```bash
git add src/remem/agents/ CLAUDE.md tests/test_memory_capability.py
git commit -m "feat: memory_dir, the seventh probed adapter capability

Keyed on the working directory, not the project: Claude Code's slug
changes in a worktree where remem's project name does not, and this
repo's own development happens in one."
```

---

### Task 6: The sync

Everything before this was parts. This applies the six cases.

**Files:**
- Modify: `src/remem/services/memory.py`
- Test: `tests/test_memory_sync.py`

**Interfaces:**
- Consumes: `memory_file.*`, `memory.classify`, `memory.load_watermarks`, `memory.save_watermarks`, `memory.designation`, `kb.resolve`, `write.remember`, `write.supersede`, `store.set_superseded`.
- Produces:
  - `memory.MEM_TAG_PREFIX = "mem:"`
  - `memory.Report` - dataclass with int fields `adopted`, `healed`, `edited`, `regenerated`, `deleted`, `unchanged`, `conflicts: list[str]`, `failures: list[tuple[str, str]]`
  - `memory.sync(store, owner_id, *, project: str, directory: Path, dry_run: bool = False) -> Report`

- [ ] **Step 1: Write the failing test**

Create `tests/test_memory_sync.py`. Every test is `@pytest.mark.db` because
`sync` reads and writes entries.

```python
from __future__ import annotations

import pytest

from remem import memory_file
from remem.services import kb, memory
from remem.services.write import remember, supersede

pytestmark = pytest.mark.db


def _designated(store, owner, project="proj", slug="proj-memory"):
    kb.create(store, owner.id, slug=slug, name="Memory", project=project)
    memory.designate(store, owner.id, project, slug)
    return slug


def _write_file(directory, name, description, body, type_="project"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        memory_file.render(
            memory_file.MemoryFile(
                name=name, title=name, description=description,
                type=type_, body=body,
            )
        )
    )


def test_an_undesignated_project_writes_nothing(store, owner, tmp_path):
    with pytest.raises(memory.NotDesignated):
        memory.sync(store, owner.id, project="proj", directory=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_case_1_a_stray_file_is_adopted(store, owner, tmp_path):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)
    assert report.adopted == 1
    entries = kb.resolve(store, owner.id, "proj-memory")
    assert [e.title for e in entries] == ["a-fact"]
    assert entries[0].summary == "a hook"
    assert "mem:a-fact" in entries[0].tags


def test_case_2_a_lost_watermark_heals_when_bodies_match(
    store, owner, tmp_path
):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    (tmp_path / memory.WATERMARK_NAME).unlink()
    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)
    assert report.healed == 1
    assert report.conflicts == []


def test_case_2_a_lost_watermark_conflicts_when_bodies_differ(
    store, owner, tmp_path
):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    (tmp_path / memory.WATERMARK_NAME).unlink()
    _write_file(tmp_path, "a-fact", "a hook", "edited body\n")
    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)
    assert report.conflicts == ["a-fact"]


def test_case_3_an_edited_file_supersedes_the_entry_keeping_its_tag(
    store, owner, tmp_path
):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    _write_file(tmp_path, "a-fact", "a hook", "edited body\n")
    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)
    assert report.edited == 1
    entries = kb.resolve(store, owner.id, "proj-memory")
    assert entries[0].body == "edited body\n"
    assert "mem:a-fact" in entries[0].tags


def test_case_4_an_edited_entry_regenerates_the_file(store, owner, tmp_path):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    entry = kb.resolve(store, owner.id, "proj-memory")[0]
    supersede(store, owner.id, entry.id, title=entry.title, body="from remem\n")
    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)
    assert report.regenerated == 1
    assert "from remem" in (tmp_path / "a-fact.md").read_text()


def test_case_5_both_sides_moved_is_a_conflict_and_writes_nothing(
    store, owner, tmp_path
):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    entry = kb.resolve(store, owner.id, "proj-memory")[0]
    supersede(store, owner.id, entry.id, title=entry.title, body="from remem\n")
    _write_file(tmp_path, "a-fact", "a hook", "from claude\n")

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.conflicts == ["a-fact"]
    assert "from claude" in (tmp_path / "a-fact.md").read_text()
    assert "from remem" in (tmp_path / "a-fact.remem-conflict.md").read_text()


def test_case_6_an_entry_out_of_the_collection_deletes_its_file(
    store, owner, tmp_path
):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    entry = kb.resolve(store, owner.id, "proj-memory")[0]
    kb.set_query(store, owner.id, "proj-memory", tags=["nothing-matches-this"])

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.deleted == 1
    assert not (tmp_path / "a-fact.md").exists()


def test_a_file_that_moved_is_never_deleted(store, owner, tmp_path):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    kb.set_query(store, owner.id, "proj-memory", tags=["nothing-matches-this"])
    _write_file(tmp_path, "a-fact", "a hook", "hand edited\n")

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.deleted == 0
    assert (tmp_path / "a-fact.md").exists()
    assert report.conflicts == ["a-fact"]


def test_a_second_sync_writes_nothing_at_all(store, owner, tmp_path):
    # The property everything else rests on. If this fails, the round-trip
    # is not byte-exact and every sync will churn.
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    before = {
        p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()
    }

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    after = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert after == before
    assert report.unchanged == 1
    assert (report.adopted, report.edited, report.regenerated,
            report.deleted) == (0, 0, 0, 0)


def test_dry_run_reports_without_writing(store, owner, tmp_path):
    _designated(store, owner)
    _write_file(tmp_path, "a-fact", "a hook", "the body\n")
    report = memory.sync(
        store, owner.id, project="proj", directory=tmp_path, dry_run=True,
    )
    assert report.adopted == 1
    assert kb.resolve(store, owner.id, "proj-memory") == []
    assert not (tmp_path / memory.WATERMARK_NAME).exists()


def test_memory_md_is_written_and_indexes_every_file(store, owner, tmp_path):
    _designated(store, owner)
    _write_file(tmp_path, "b-fact", "second", "b\n")
    _write_file(tmp_path, "a-fact", "first", "a\n")
    memory.sync(store, owner.id, project="proj", directory=tmp_path)
    index = (tmp_path / "MEMORY.md").read_text()
    assert index.index("a-fact.md") < index.index("b-fact.md")


def test_a_malformed_file_is_reported_and_costs_nothing_else(
    store, owner, tmp_path
):
    _designated(store, owner)
    _write_file(tmp_path, "good", "a hook", "fine\n")
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "bad.md").write_text("no frontmatter here\n")

    report = memory.sync(store, owner.id, project="proj", directory=tmp_path)

    assert report.adopted == 1
    assert [name for name, _ in report.failures] == ["bad"]
```

Read `kb.set_query`'s real signature before writing the two tests that call
it and match it; if it takes a `CollectionQuery` rather than keyword
arguments, build one.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_memory_sync.py -v`
Expected: FAIL - `AttributeError: module 'remem.services.memory' has no attribute 'sync'`

- [ ] **Step 3: Implement the sync**

Add to `src/remem/services/memory.py`:

```python
#: Identity. One file, one entry, one tag - following handoff's `topic:` and
#: ingest's `src:`/`sec:`. The slug survives a rename of the file itself and
#: is what `[[wiki-links]]` resolve against.
MEM_TAG_PREFIX = "mem:"


@dataclass(slots=True)
class Report:
    adopted: int = 0
    healed: int = 0
    edited: int = 0
    regenerated: int = 0
    deleted: int = 0
    unchanged: int = 0
    conflicts: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)


def _name_of(entry) -> str | None:
    for tag in entry.tags:
        if tag.startswith(MEM_TAG_PREFIX):
            return tag[len(MEM_TAG_PREFIX):]
    return None


def sync(
    store: Store,
    owner_id: UUID,
    *,
    project: str,
    directory: Path,
    dry_run: bool = False,
) -> Report:
    """Adopt what Claude wrote, then regenerate the directory from the store.

    Adoption comes first on purpose. The directory has a second writer that
    cannot be told to stop, so regenerating without adopting would destroy
    every memory written since the last run.
    """
    slug = designation(store, owner_id, project)
    if slug is None:
        raise NotDesignated(project)

    report = Report()
    marks = load_watermarks(directory)
    index = {}
    index_path = directory / "MEMORY.md"
    if index_path.exists():
        index = memory_file.parse_index(index_path.read_text())

    # --- read both sides ------------------------------------------------
    files: dict[str, memory_file.MemoryFile] = {}
    if directory.exists():
        for path in sorted(directory.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            try:
                files[path.stem] = memory_file.parse(
                    path.read_text(),
                    name=path.stem,
                    title=index.get(path.name, ""),
                )
            except (memory_file.MalformedMemoryFile, OSError) as exc:
                # Collect rather than abort: one bad file must not cost the
                # other thirty-seven.
                report.failures.append((path.stem, str(exc)))

    entries = {
        name: e
        for e in kb.resolve(store, owner_id, slug)
        if (name := _name_of(e)) is not None
    }

    # --- classify and apply ---------------------------------------------
    now = datetime.now(timezone.utc).isoformat()
    for name in sorted(set(files) | set(entries)):
        mf = files.get(name)
        entry = entries.get(name)
        case = classify(
            file_sha=None if mf is None else memory_file.body_sha(mf.body),
            entry_sha=None if entry is None else memory_file.body_sha(entry.body),
            mark=marks.get(name),
        )

        if case is Case.CONFLICT:
            report.conflicts.append(name)
            if not dry_run and entry is not None:
                (directory / f"{name}.remem-conflict.md").write_text(
                    memory_file.render(_as_file(entry, name))
                )
            continue

        if case is Case.UNCHANGED:
            report.unchanged += 1
            continue

        if case is Case.ADOPT_NEW:
            report.adopted += 1
            if not dry_run:
                new = remember(
                    store, owner_id,
                    title=mf.title or memory_file.title_from_name(name),
                    body=mf.body,
                    summary=mf.description,
                    kind=Kind.NOTE,
                    project=project,
                    tags=_tags_for(mf, name),
                    origin=Origin.AGENT,
                )
                kb.pin(store, owner_id, slug, new.id)
                marks[name] = Watermark(
                    entry_id=str(new.id),
                    body_sha=memory_file.body_sha(mf.body),
                    exported_at=now,
                )
            continue

        if case is Case.ADOPT_EDIT:
            if entry is None:
                # The file was edited for an entry that has left the
                # collection. Re-adopting would duplicate the entry that
                # still exists outside it, and deleting would destroy the
                # edit, so this is a conflict: report it, touch nothing.
                report.conflicts.append(name)
                continue
            report.edited += 1
            if not dry_run:
                new = supersede(
                    store, owner_id, entry.id,
                    title=mf.title or entry.title,
                    body=mf.body,
                    summary=mf.description,
                )
                marks[name] = Watermark(
                    entry_id=str(new.id),
                    body_sha=memory_file.body_sha(mf.body),
                    exported_at=now,
                )
            continue

        if case is Case.HEAL:
            report.healed += 1
            if not dry_run:
                marks[name] = Watermark(
                    entry_id=str(entry.id),
                    body_sha=memory_file.body_sha(entry.body),
                    exported_at=now,
                )
            continue

        if case is Case.REGENERATE:
            report.regenerated += 1
            if not dry_run:
                directory.mkdir(parents=True, exist_ok=True)
                (directory / f"{name}.md").write_text(
                    memory_file.render(_as_file(entry, name))
                )
                marks[name] = Watermark(
                    entry_id=str(entry.id),
                    body_sha=memory_file.body_sha(entry.body),
                    exported_at=now,
                )
            continue

        if case is Case.DELETE:
            report.deleted += 1
            if not dry_run:
                (directory / f"{name}.md").unlink(missing_ok=True)
                marks.pop(name, None)

    if not dry_run:
        directory.mkdir(parents=True, exist_ok=True)
        # Re-read from the store rather than reusing `entries`, so the index
        # includes anything adopted in this run.
        live = [
            _as_file(e, n)
            for e in kb.resolve(store, owner_id, slug)
            if (n := _name_of(e)) is not None
        ]
        index_path.write_text(memory_file.render_index(live))
        save_watermarks(directory, marks)

    return report


def _tags_for(mf: memory_file.MemoryFile, name: str) -> list[str]:
    tags = [f"{MEM_TAG_PREFIX}{name}"]
    if mf.type:
        tags.append(f"type:{mf.type}")
    return tags


def _as_file(entry, name: str) -> memory_file.MemoryFile:
    type_ = None
    for tag in entry.tags:
        if tag.startswith("type:"):
            type_ = tag[len("type:"):]
    return memory_file.MemoryFile(
        name=name,
        title=entry.title,
        description=entry.summary or "",
        type=type_,
        body=entry.body,
    )
```

The code block is authoritative over any prose in this task. If you believe
the two disagree, say so in your task report rather than guessing.

Add the imports this needs at the top of the module: `field` from
`dataclasses`, `datetime`/`timezone` from `datetime`, `Path` from `pathlib`,
`Kind` and `Origin` from `remem.domain`, `memory_file` from `remem`, and
`remember`/`supersede` from `remem.services.write`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_memory_sync.py -v`
Expected: 13 passed. Count the test functions in the file by name and state
the number in your report - the file above specifies **13**, and a lower
number means one was lost in transcription.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest`
Expected: 0 failures, 0 skips.

- [ ] **Step 6: Commit**

```bash
git add src/remem/services/memory.py tests/test_memory_sync.py
git commit -m "feat: memory sync, adopting before it regenerates

Six cases over a watermark. Adoption is first because the directory has
a second writer that cannot be told to stop, so regenerating first would
destroy every memory written since the last run."
```

---

### Task 7: The CLI

Three commands. The frontend resolves a working directory and a project,
calls the service, prints counts, and decides nothing.

**Files:**
- Modify: `src/remem/cli.py`
- Modify: `src/remem/services/memory.py` (the `status` reader)
- Modify: `CLAUDE.md` (a new "### Claude Code memory" section)
- Test: `tests/test_memory_cli.py`

**Interfaces:**
- Consumes: `memory.sync`, `memory.designate`, `memory.designation`, the claude-code adapter's `memory_dir`.
- Produces:
  - `memory.Status` - dataclass with fields `project: str`, `collection: str | None`, `directory: Path | None`, `entries: int`, `files: int`, `stale: int`, `overlap: int`, `overlap_bytes: int`
  - `memory.status(store, owner_id, *, project: str, directory: Path | None, kb_slug: str | None = None) -> Status`
  - CLI: `remem memory designate`, `remem memory sync`, `remem memory status`

- [ ] **Step 1: Write the failing test**

Create `tests/test_memory_cli.py`. These use the CLI runner pattern the
existing CLI tests use - read `tests/test_cli_ingest.py` (or whichever file
covers `remem ingest`) and copy its fixtures exactly, including the `live_dsn`
"env" fixture, because these commands open their own session.

```python
from __future__ import annotations

import pytest

pytestmark = pytest.mark.db


def test_designate_requires_an_existing_collection(runner, env):
    result = runner.invoke(app, ["memory", "designate", "nope"], env=env)
    assert result.exit_code == 1
    assert "nope" in result.stderr


def test_sync_on_an_undesignated_project_says_so_and_exits_nonzero(
    runner, env
):
    result = runner.invoke(app, ["memory", "sync"], env=env)
    assert result.exit_code == 1
    assert "designate" in result.stderr


def test_sync_prints_counts(runner, env, memory_dir_with_one_stray):
    result = runner.invoke(app, ["memory", "sync"], env=env)
    assert result.exit_code == 0
    assert "1 adopted" in result.stdout


def test_sync_exits_nonzero_when_a_file_is_left_in_conflict(
    runner, env, memory_dir_in_conflict
):
    result = runner.invoke(app, ["memory", "sync"], env=env)
    assert result.exit_code == 1
    # Counts land on stdout, failures on stderr. Asserting the wrong stream
    # is the mistake this repo has already made once with `remem ingest`.
    assert "conflict" in result.stderr


def test_status_reports_the_designation_and_the_overlap(runner, env):
    result = runner.invoke(app, ["memory", "status"], env=env)
    assert result.exit_code == 0
    assert "not designated" in result.stdout
```

Write the two fixtures (`memory_dir_with_one_stray`, `memory_dir_in_conflict`)
in this test file, building the directory with `memory_file.render` the way
`tests/test_memory_sync.py` does, and pointing `CLAUDE_CONFIG_DIR` at
`tmp_path` through `env` so the adapter resolves the directory there.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_memory_cli.py -v`
Expected: FAIL - `No such command 'memory'`

- [ ] **Step 3: Add the status reader to the service**

```python
@dataclass(slots=True)
class Status:
    project: str
    collection: str | None
    directory: Path | None
    entries: int = 0
    files: int = 0
    #: Files whose checksum no longer matches their watermark - what the next
    #: sync would adopt or flag.
    stale: int = 0
    #: Entries in both the memory collection and the project's knowledge
    #: base. Not an error: overlapping deliberately is a legitimate choice.
    #: Reported because MEMORY.md and the SessionStart block are both loaded
    #: every session, and RulesExceedBudget fails silently on every harness,
    #: so a number you can see is the cheapest guard available.
    overlap: int = 0
    overlap_bytes: int = 0


def status(
    store: Store,
    owner_id: UUID,
    *,
    project: str,
    directory: Path | None,
    kb_slug: str | None = None,
) -> Status:
    slug = designation(store, owner_id, project)
    out = Status(project=project, collection=slug, directory=directory)
    if slug is None:
        return out

    entries = kb.resolve(store, owner_id, slug)
    out.entries = len(entries)

    if directory is not None and directory.exists():
        marks = load_watermarks(directory)
        for path in directory.glob("*.md"):
            if path.name == "MEMORY.md":
                continue
            out.files += 1
            mark = marks.get(path.stem)
            try:
                mf = memory_file.parse(
                    path.read_text(), name=path.stem, title="",
                )
            except (memory_file.MalformedMemoryFile, OSError):
                out.stale += 1
                continue
            if mark is None or mark.body_sha != memory_file.body_sha(mf.body):
                out.stale += 1

    if kb_slug:
        try:
            kb_ids = {e.id for e in kb.resolve(store, owner_id, kb_slug)}
        except kb.CollectionNotFound:
            return out
        shared = [e for e in entries if e.id in kb_ids]
        out.overlap = len(shared)
        out.overlap_bytes = sum(len(e.body.encode("utf-8")) for e in shared)
    return out
```

- [ ] **Step 4: Add the CLI**

In `src/remem/cli.py`, beside the other sub-apps:

```python
memory_app = typer.Typer(help="Claude Code's memory directory, from remem.")
app.add_typer(memory_app, name="memory")


def _memory_dir(project: str | None) -> Path | None:
    """The adapter's answer, or None. Probed, like every optional capability."""
    adapter = registry.get("claude-code")
    fn = getattr(adapter, "memory_dir", None)
    if fn is None:
        return None
    try:
        return fn(Path.cwd())
    except Exception as exc:  # noqa: BLE001 - warn and degrade, never crash
        typer.echo(f"claude-code: memory_dir failed: {exc}", err=True)
        return None


@memory_app.command("designate")
def memory_designate(
    slug: Annotated[Optional[str], typer.Argument()] = None,
    clear: Annotated[bool, typer.Option("--none")] = False,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Point this project's memory export at an existing collection."""
    resolved = _resolve_project(project, False)
    with _session() as s:
        try:
            memory_service.designate(
                s.store, s.owner.id, resolved, None if clear else slug,
            )
        except kb.CollectionNotFound as exc:
            typer.echo(
                f"No collection {exc}. Create it with `remem kb create` "
                f"first - designating cannot create one, because a "
                f"collection with an empty query matches nothing forever.",
                err=True,
            )
            raise typer.Exit(1)
    typer.echo("Cleared." if clear else f"{resolved} -> {slug}")


@memory_app.command("sync")
def memory_sync(
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Adopt what Claude wrote, then regenerate the directory from remem."""
    resolved = _resolve_project(project, False)
    directory = _memory_dir(resolved)
    if directory is None:
        typer.echo("claude-code has no memory directory here.", err=True)
        raise typer.Exit(1)
    with _session() as s:
        try:
            report = memory_service.sync(
                s.store, s.owner.id, project=resolved,
                directory=directory, dry_run=dry_run,
            )
        except memory_service.NotDesignated:
            typer.echo(
                f"{resolved} has no memory collection. "
                f"Run `remem memory designate <slug>` first.",
                err=True,
            )
            raise typer.Exit(1)
    prefix = "Would write: " if dry_run else ""
    typer.echo(
        f"{prefix}{report.adopted} adopted, {report.edited} edited, "
        f"{report.regenerated} regenerated, {report.healed} healed, "
        f"{report.deleted} deleted, {report.unchanged} unchanged."
    )
    for name in report.conflicts:
        typer.echo(
            f"conflict: {name} changed on both sides; "
            f"remem's version is in {name}.remem-conflict.md",
            err=True,
        )
    for name, reason in report.failures:
        typer.echo(f"failed: {name}: {reason}", err=True)
    if report.conflicts or report.failures:
        # Fail-loud: a person typed this, and a conflict is a definite
        # statement that work was not done - not an "I could not tell".
        raise typer.Exit(1)


@memory_app.command("status")
def memory_status(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """What is designated, what is on disk, and what overlaps the KB."""
    resolved = _resolve_project(project, False)
    directory = _memory_dir(resolved)
    with _session() as s:
        st = memory_service.status(
            s.store, s.owner.id, project=resolved,
            directory=directory, kb_slug=resolved,
        )
    if st.collection is None:
        typer.echo(f"{resolved}: not designated.")
        return
    typer.echo(f"{resolved}: {st.collection} -> {st.directory}")
    typer.echo(f"  {st.entries} entries, {st.files} files, {st.stale} stale")
    if st.overlap:
        typer.echo(
            f"  {st.overlap} entries ({st.overlap_bytes} bytes) are also in "
            f"the '{resolved}' knowledge base, so they load twice per session"
        )
```

Import `memory as memory_service` from `remem.services`, `kb`, and
`registry`, following the file's existing import style.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_memory_cli.py -v`
Expected: 5 passed.

- [ ] **Step 6: Document it in CLAUDE.md**

Add a section after "### Ingested documents":

```markdown
### Claude Code memory

`remem memory sync` owns `~/.claude/projects/<cwd-slug>/memory/` - Claude
Code's file-based memory - as a generated view of a designated collection.
Unlike opencode's `remem.js` and cursor's `remem.mdc`, this generated file
set has a second writer that cannot be told to stop, so the sync **adopts
before it regenerates**: anything on disk remem has not seen becomes an
entry first.

- Opt-in per project, holding a value rather than a boolean: which
  collection. An undesignated project generates nothing, which is what
  keeps `MEMORY.md` from double-loading against the `SessionStart` block.
- `.remem-sync.json` is what makes "which side moved" answerable. This is
  deliberately the opposite of `ingest`, which compares bodies and stores no
  hash - ingest has one writer, so "differs" and "the file changed" are the
  same statement. Here both sides write.
- Two gates and they are the whole safety story: a file whose checksum does
  not match its watermark is never deleted, and a file changed on both sides
  is never overwritten. Conflicts write remem's version alongside as
  `<name>.remem-conflict.md` and exit non-zero.
- `memory_file.py` is pure, so its tests carry no `db` marker and run on CI.
  `render(parse(f)) == f` byte for byte over a corpus copied from the real
  directory is load-bearing, not cosmetic: the sync decides "unchanged" by
  checksum, so a renderer that normalised whitespace would report a change
  on every file forever.
- The generated `MEMORY.md` uses an em dash between link and hook, against
  this repo's convention, because that line's format belongs to Claude Code.
- Not in `remem doctor`: the designation lives in the database and doctor
  opens no connection. The overlap count lives in `remem memory status`.
```

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest`
Expected: 0 failures, 0 skips. Report the total and the delta from the
baseline you recorded at Task 1.

- [ ] **Step 8: Commit**

```bash
git add src/remem/cli.py src/remem/services/memory.py CLAUDE.md tests/test_memory_cli.py
git commit -m "feat: remem memory designate | sync | status

Fail-loud, like ingest and embed: a person typed it. A conflict exits
non-zero because it is a definite statement that work was not done."
```

---

## Manual verification

After Task 7, prove it against the real directory rather than a fixture.
Do this on a **copy** first:

```bash
cp -r ~/.claude/projects/-Users-brandon-llmworkspace-remem/memory /tmp/memory-backup
remem kb create remem-memory --name "remem memory" --project remem
remem memory designate remem-memory
remem memory status
remem memory sync --dry-run
remem memory sync
remem memory sync          # must report everything unchanged
remem memory status
git -C ~/.claude diff --stat 2>/dev/null || true
diff -r /tmp/memory-backup ~/.claude/projects/-Users-brandon-llmworkspace-remem/memory
```

The second `sync` reporting anything other than all-unchanged means the
round-trip is not byte-exact. Fix that before trusting it with the other 12
directories. Check `remem kb create`'s real flags first; the invocation above
is the expected shape, not a verified one.
