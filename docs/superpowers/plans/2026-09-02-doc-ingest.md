# Doc Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest markdown documents into remem as heading-sized, searchable entries that can be re-ingested without duplicating or stranding anything.

**Architecture:** A pure chunker (`markdown.py`, no I/O) splits a document on `h1`-`h3` into one anchor chunk plus one chunk per heading. A service (`services/ingest.py`) reads files, diffs each chunk against the live entry with matching `src:`/`sec:` tags, and writes only what changed. Chunks that vanished from a file are superseded by that file's anchor. Two new origins ride the existing `DEFAULT_ORIGINS` allowlist to keep plans out of default results.

**Tech Stack:** Python 3.14, Typer, psycopg 3, Postgres 18, pytest.

**Spec:** `docs/superpowers/specs/2026-09-01-doc-ingest-design.md`

## Global Constraints

- Python 3.14. `from __future__ import annotations` at the top of every module.
- Strict layering, downward calls only: `cli.py`/`mcp_server.py` -> `services/` -> `store.py` -> `domain.py`. No policy branch in a frontend.
- `markdown.py` is pure: no file I/O, no store, no config. It is handed text and returns chunks.
- Prose and comments use spaced hyphens ` - `, never em dashes.
- Comments explain *why*, at length, wherever a decision looks arbitrary.
- `MAX_BODY = 32 * 1024` (bytes). Bodies over it are truncated with a pointer, never split.
- `MAX_CHUNKS_PER_FILE = 200`. Exceeding it raises; it must never silently truncate a sweep.
- Migrations: add a new numbered file, never edit an applied one.
- Tests that need Postgres carry `pytest.mark.db`. Tests that do not must NOT carry it - a marker that hides a test on CI is a known failure mode in this repo.

---

### Task 1: Two origins and the migration that allows them

**Files:**
- Create: `src/remem/backends/postgres/migrations/012_ingest_origins.sql`
- Modify: `src/remem/domain.py` (the `Origin` enum)
- Modify: `src/remem/services/search.py` (`DEFAULT_ORIGINS`)
- Test: `tests/test_ingest_origins.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Origin.INGESTED` (value `"ingested"`), `Origin.ARCHIVED` (value `"archived"`); `search.DEFAULT_ORIGINS` containing `HUMAN, AGENT, EXTRACTED, INGESTED`; `search.find(..., include_archived: bool = False)`.

- [ ] **Step 1: Write the failing test**

`tests/test_ingest_origins.py`:

```python
"""Visibility rules for the two ingest origins.

Mirrors test_extract_origins.py and test_handoff_visibility.py. The
DEFAULT_ORIGINS comment in services/search.py warns that a new origin not
added to that list vanishes from search silently; this is the test that
makes the warning bite.
"""

from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import CollectionQuery, Kind, Origin, Query
from remem.services import kb, search
from remem.services.write import remember

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def test_ingested_is_in_the_default_origins_and_archived_is_not():
    assert Origin.INGESTED in search.DEFAULT_ORIGINS
    assert Origin.ARCHIVED not in search.DEFAULT_ORIGINS


def test_default_search_finds_ingested_and_hides_archived(store, owner):
    remember(store, owner.id, title="Sweep design", body="the sweep supersedes orphans",
             kind=Kind.DOC, project="remem", origin=Origin.INGESTED)
    remember(store, owner.id, title="Task 9 sweep", body="the sweep supersedes orphans",
             kind=Kind.DOC, project="remem", origin=Origin.ARCHIVED)

    titles = [h.entry.title for h in search.find(store, owner.id, Query(text="sweep"))]
    assert titles == ["Sweep design"]


def test_include_archived_surfaces_both(store, owner):
    remember(store, owner.id, title="Sweep design", body="the sweep supersedes orphans",
             kind=Kind.DOC, project="remem", origin=Origin.INGESTED)
    remember(store, owner.id, title="Task 9 sweep", body="the sweep supersedes orphans",
             kind=Kind.DOC, project="remem", origin=Origin.ARCHIVED)

    hits = search.find(store, owner.id, Query(text="sweep"), include_archived=True)
    assert {h.entry.title for h in hits} == {"Sweep design", "Task 9 sweep"}


def test_neither_origin_reaches_a_context_block(store, owner):
    remember(store, owner.id, title="Ingested rule", body="never in context",
             kind=Kind.RULE, project="remem", origin=Origin.INGESTED)
    remember(store, owner.id, title="Archived rule", body="never in context",
             kind=Kind.RULE, project="remem", origin=Origin.ARCHIVED)
    kb.create(store, owner.id, slug="remem", title="remem",
              query=CollectionQuery(project="remem"))

    # kb.resolve takes the collection's SLUG, not the object.
    entries = kb.resolve(store, owner.id, "remem")
    assert entries == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ingest_origins.py -v`
Expected: FAIL with `AttributeError: INGESTED` on the enum.

If instead every test SKIPS, Postgres is not running. Start it with `docker compose up -d` and re-run. A skip is not a pass.

- [ ] **Step 3: Add the enum members**

In `src/remem/domain.py`, extend `Origin`:

```python
class Origin(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    #: Written by the extractor from a session's events. Was 'capture'.
    EXTRACTED = "extracted"
    HANDOFF = "handoff"
    #: A chunk of a markdown document loaded by `remem ingest`. In
    #: DEFAULT_ORIGINS: this is the reasoning layer, and it is what ingest
    #: exists to make searchable.
    INGESTED = "ingested"
    #: An ingested chunk held out of default results - implementation plans,
    #: whose text is mostly source code that now lives in src/. Not in
    #: DEFAULT_ORIGINS; reachable with --archived.
    ARCHIVED = "archived"
```

- [ ] **Step 4: Write the migration**

`src/remem/backends/postgres/migrations/012_ingest_origins.sql`:

```sql
-- Ingested document chunks are ordinary entries with their own origins, so
-- they can be held out of context blocks and (for plans) out of default
-- search without a second table.
--
-- Two values rather than one: origin is the only default-hiding mechanism
-- remem has, because DEFAULT_ORIGINS is an allowlist and an exclude filter
-- was deliberately declined (see services/search.py). 'ingested' is in that
-- list, 'archived' is not.
--
-- This migration adds values and writes NO row that carries them. That is
-- required, not stylistic: as 005_handoff.sql records, a value added by
-- `add value` cannot be USED in the transaction that added it unless the
-- type was created there too, and migrate() runs every pending migration in
-- one transaction.
--
-- `if not exists` because a database migrated by a build that already
-- carried these values must not fail here.
alter type entry_origin add value if not exists 'ingested';
alter type entry_origin add value if not exists 'archived';
```

- [ ] **Step 5: Extend DEFAULT_ORIGINS and add the flag**

In `src/remem/services/search.py`, replace the `DEFAULT_ORIGINS` assignment and its comment:

```python
#: What a search returns when the caller did not ask for specific origins.
#: Handoffs are excluded: a project hands off dozens of times and every one of
#: them would otherwise sit on top of the results. Archived document chunks
#: are excluded for the neighbouring reason - they are the minority by count
#: (111 chunks against 211 at the time of writing) but three times the volume,
#: and what they contain is executed plan steps and source code that now lives
#: in src/.
#:
#: This list must gain any future origin, or that origin silently vanishes
#: from search. The alternative - an `exclude_origins` field on Query - avoids
#: that at the cost of a second overlapping filter in the store's SQL for one
#: caller. Chosen deliberately; if a sixth origin appears, look here.
DEFAULT_ORIGINS = [Origin.HUMAN, Origin.AGENT, Origin.EXTRACTED, Origin.INGESTED]
```

Then extend `find`. Add the parameter alongside `include_handoffs`:

```python
def find(
    store: Store,
    owner_id: UUID,
    query: Query,
    *,
    ...
    include_handoffs: bool = False,
    include_archived: bool = False,
    ...
) -> list[Hit]:
```

and replace the origin-defaulting block (currently `if not include_handoffs and not query.origins:`) with:

```python
    if not query.origins:
        # An explicit origins list is the caller saying exactly what they
        # want; never widen or narrow it behind their back. Otherwise start
        # from the default allowlist and add back what was asked for.
        origins = list(DEFAULT_ORIGINS)
        if include_handoffs:
            origins.append(Origin.HANDOFF)
        if include_archived:
            origins.append(Origin.ARCHIVED)
        query = replace(query, origins=origins)
```

Update the docstring line to read:

```
    Handoffs and archived document chunks are excluded from the default
    origins unless `include_handoffs` / `include_archived` is set, or the
    caller already named specific origins.
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_ingest_origins.py tests/test_handoff_visibility.py tests/test_extract_origins.py -v`
Expected: PASS, no skips. The two existing files must still pass - Step 5 rewrote the code path they cover.

- [ ] **Step 7: Run the full suite and check the skip count**

Run: `uv run pytest -q`
Expected: all pass. Note the skip count; a green run with everything skipped means Postgres is down.

- [ ] **Step 8: Commit**

```bash
git add src/remem/domain.py src/remem/services/search.py \
        src/remem/backends/postgres/migrations/012_ingest_origins.sql \
        tests/test_ingest_origins.py
git commit -m "feat: add ingested and archived origins"
```

---

### Task 2: The chunker

**Files:**
- Create: `src/remem/markdown.py`
- Test: `tests/test_markdown_chunker.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `markdown.MAX_BODY: int` (32768)
  - `markdown.slugify(text: str) -> str`
  - `markdown.Chunk` - frozen dataclass with fields `slug: str`, `title: str`, `body: str`, `anchor: bool`
  - `markdown.split(text: str, *, doc_title: str) -> list[Chunk]` - returns the anchor chunk first, then one chunk per `h1`-`h3` heading, in document order.

- [ ] **Step 1: Write the failing tests**

`tests/test_markdown_chunker.py`. Note there is no `pytestmark` - this module is pure and must run without Postgres:

```python
"""The chunker is pure - no store, no I/O - so these tests carry no db
marker and run everywhere. That split is deliberate: this repo has twice
been bitten by markers that hid a test on CI."""

from __future__ import annotations

from remem.markdown import MAX_BODY, Chunk, slugify, split


def test_slugify_lowercases_and_hyphenates():
    assert slugify("Invariants worth not breaking") == "invariants-worth-not-breaking"
    assert slugify("`--archive` is a flag, not a path heuristic") == "archive-is-a-flag-not-a-path-heuristic"
    assert slugify("Two   spaces") == "two-spaces"


def test_the_first_chunk_is_the_anchor_and_carries_the_lead_paragraph():
    text = "# Ingest design\n\nDesign, 2026-09-01.\n\n## Problem\n\nnone of it is in remem\n"
    chunks = split(text, doc_title="ingest design")

    assert chunks[0].anchor is True
    assert chunks[0].slug == ""
    assert chunks[0].title == "ingest design"
    assert "Design, 2026-09-01." in chunks[0].body
    assert "none of it is in remem" not in chunks[0].body


def test_one_chunk_per_heading_titled_with_the_document():
    text = "# Doc\n\nlead\n\n## Alpha\n\nbody a\n\n## Beta\n\nbody b\n"
    chunks = split(text, doc_title="doc")

    assert [c.slug for c in chunks] == ["", "alpha", "beta"]
    assert chunks[1].title == "doc § Alpha"
    assert chunks[1].body.strip() == "## Alpha\n\nbody a"


def test_a_hash_inside_a_code_fence_is_not_a_heading():
    text = (
        "# Doc\n\nlead\n\n## Alpha\n\n"
        "```python\n"
        "# this is a comment, not a heading\n"
        "x = 1\n"
        "```\n\n"
        "still alpha\n"
    )
    chunks = split(text, doc_title="doc")

    assert [c.slug for c in chunks] == ["", "alpha"]
    assert "still alpha" in chunks[1].body


def test_headings_deeper_than_h3_stay_inside_their_section():
    text = "# Doc\n\nlead\n\n## Alpha\n\n#### Deep\n\ndeep body\n"
    chunks = split(text, doc_title="doc")

    assert [c.slug for c in chunks] == ["", "alpha"]
    assert "#### Deep" in chunks[1].body


def test_a_file_with_no_headings_is_one_chunk_plus_the_anchor():
    chunks = split("just prose, no headings at all\n", doc_title="notes")

    assert [c.slug for c in chunks] == ["", "notes"]
    assert chunks[1].body.strip() == "just prose, no headings at all"


def test_duplicate_headings_get_distinct_slugs():
    text = "# Doc\n\nlead\n\n## Notes\n\nfirst\n\n## Notes\n\nsecond\n"
    chunks = split(text, doc_title="doc")

    assert [c.slug for c in chunks] == ["", "notes", "notes-2"]


def test_an_oversized_body_is_truncated_not_split():
    text = "# Doc\n\nlead\n\n## Big\n\n" + ("x" * (MAX_BODY * 2)) + "\n"
    chunks = split(text, doc_title="doc")

    assert [c.slug for c in chunks] == ["", "big"]
    assert len(chunks[1].body.encode()) <= MAX_BODY
    assert chunks[1].body.endswith("[truncated]")


def test_chunks_are_frozen():
    chunk = Chunk(slug="a", title="t", body="b", anchor=False)
    try:
        chunk.slug = "b"
    except AttributeError:
        return
    raise AssertionError("Chunk must be frozen")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_markdown_chunker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'remem.markdown'`.

- [ ] **Step 3: Write the chunker**

`src/remem/markdown.py`:

```python
"""Splitting a markdown document into chunk-sized pieces.

Pure: handed text, returns chunks. No file I/O, no store, no config - which
is what lets its tests run without Postgres.

Splitting is on headings and ONLY on headings. The obvious alternative, a
size-based sub-splitter for long sections, was rejected in the design: a
plan section is mostly fenced code, so a size split cuts through the middle
of a fence and yields a chunk that starts mid-function with an unterminated
backtick. A 15KB section that is one coherent unit is better than four
incoherent ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Bodies larger than this are truncated with a pointer, never split. Nothing
#: in the corpus this was written for comes close; the cap exists so a future
#: document without headings cannot write an unbounded body.
MAX_BODY = 32 * 1024

_TRUNCATED = "\n\n[truncated]"

#: h1-h3 only. Deeper headings stay inside their section: h4 in this corpus
#: marks a sub-point of an argument, not a separate one.
_HEADING = re.compile(r"^(#{1,3})\s+(.*\S)\s*$")

#: A fence opener or closer. Tracking these is the ONLY fence logic in the
#: design, and it exists for heading DETECTION - a '# comment' on the first
#: line of a Python block is not a section - not for fence-aware splitting.
_FENCE = re.compile(r"^\s*(```|~~~)")


def slugify(text: str) -> str:
    """A heading turned into the stable half of a chunk's identity.

    Punctuation is dropped rather than encoded: `sec:` tags are read by
    people in search output, and a heading's backticks and commas carry no
    identity that its words do not.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return cleaned.strip("-")


@dataclass(frozen=True, slots=True)
class Chunk:
    """One entry's worth of a document.

    Frozen because a chunk is a reading of a file at a moment. Ingest
    compares chunks against stored bodies and must never be able to edit one
    en route.
    """

    slug: str
    title: str
    body: str
    anchor: bool = False


def _capped(body: str) -> str:
    """Truncate on a byte budget, never mid-character."""
    encoded = body.encode()
    if len(encoded) <= MAX_BODY:
        return body
    budget = MAX_BODY - len(_TRUNCATED.encode())
    return encoded[:budget].decode(errors="ignore") + _TRUNCATED


def split(text: str, *, doc_title: str) -> list[Chunk]:
    """Anchor chunk first, then one chunk per h1-h3 heading, in order.

    The anchor is the document itself: its lead matter, everything before the
    first heading that is not the title. It is not merely a nicety - the
    sweep in services/ingest.py supersedes vanished chunks BY the anchor,
    because set_superseded needs a replacement id and a deleted heading has
    none.
    """
    lines = text.splitlines()
    fenced = False
    lead: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None

    for line in lines:
        if _FENCE.match(line):
            fenced = not fenced
        heading = None if fenced else _HEADING.match(line)
        if heading is None:
            (current if current is not None else lead).append(line)
            continue
        level, title = len(heading.group(1)), heading.group(2)
        if level == 1 and not sections and current is None:
            # The document's own h1 titles the anchor rather than opening a
            # section; doc_title is what the caller wants it called.
            continue
        current = [line]
        sections.append((title, current))

    chunks = [
        Chunk(slug="", title=doc_title, body=_capped("\n".join(lead).strip()), anchor=True)
    ]

    if not sections:
        # A file with no headings is still worth one searchable chunk. Its
        # slug is the document, so re-ingest matches it like any other.
        return chunks + [
            Chunk(slug=slugify(doc_title), title=doc_title,
                  body=_capped("\n".join(lead).strip()))
        ]

    seen: dict[str, int] = {}
    for title, body_lines in sections:
        slug = slugify(title)
        seen[slug] = seen.get(slug, 0) + 1
        if seen[slug] > 1:
            # Two headings with the same words are two chunks, and identity
            # is (src, sec) - so they need distinct slugs or the second
            # would supersede the first on every single ingest.
            slug = f"{slug}-{seen[slug]}"
        chunks.append(
            Chunk(slug=slug, title=f"{doc_title} § {title}",
                  body=_capped("\n".join(body_lines).strip()))
        )
    return chunks
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_markdown_chunker.py -v`
Expected: PASS, 9 tests, zero skips.

- [ ] **Step 5: Prove the chunker against the real corpus**

Run:

```bash
uv run python -c "
from pathlib import Path
from remem.markdown import split
for p in sorted(Path('docs/superpowers').rglob('*.md')):
    n = len(split(p.read_text(), doc_title=p.stem))
    print(f'{n:4d}  {p}')
"
```

Expected: every file yields at least 2 chunks (anchor plus one), and the totals are in the neighbourhood of the spec's table - roughly 320 chunks across the corpus. A file yielding exactly 2 that is not a stub means its headings were missed; investigate before continuing.

- [ ] **Step 6: Commit**

```bash
git add src/remem/markdown.py tests/test_markdown_chunker.py
git commit -m "feat: markdown chunker, split on headings only"
```

---

### Task 3: The ingest service - write and diff

**Files:**
- Create: `src/remem/services/ingest.py`
- Test: `tests/test_ingest_service.py`

**Interfaces:**
- Consumes: `markdown.split`, `markdown.slugify`, `markdown.Chunk`; `write.remember`, `write.supersede`; `store.search`; `domain.Query`, `domain.Kind`, `domain.Origin`.
- Produces:
  - `ingest.MAX_CHUNKS_PER_FILE: int` (200)
  - `ingest.TooManyChunks(Exception)`
  - `ingest.Report` - dataclass with `created: int`, `changed: int`, `unchanged: int`, `swept: int`, `failures: list[tuple[Path, str]]`
  - `ingest.src_tag(path: Path) -> str` and `ingest.sec_tag(slug: str) -> str`
  - `ingest.ingest_file(store, owner_id, path, *, project, archive=False, dry_run=False) -> Report`

Task 4 adds the sweep to this same module and `ingest_paths` on top of it.

- [ ] **Step 1: Write the failing tests**

`tests/test_ingest_service.py`:

```python
from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Kind, Origin, Query
from remem.services import ingest

pytestmark = pytest.mark.db

DOC = "# Design\n\nlead matter\n\n## Alpha\n\nbody a\n\n## Beta\n\nbody b\n"


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


@pytest.fixture
def doc(tmp_path):
    path = tmp_path / "design.md"
    path.write_text(DOC)
    return path


def _live(store, owner, path):
    return store.search(
        Query(tags=[ingest.src_tag(path)], origins=[Origin.INGESTED, Origin.ARCHIVED],
              limit=ingest.MAX_CHUNKS_PER_FILE),
        owner.id,
    )


def test_ingest_writes_an_anchor_and_one_entry_per_heading(store, owner, doc):
    report = ingest.ingest_file(store, owner.id, doc, project="remem")

    assert report.created == 3
    entries = [h.entry for h in _live(store, owner, doc)]
    assert {e.title for e in entries} == {"design", "design § Alpha", "design § Beta"}
    assert all(e.kind is Kind.DOC for e in entries)
    assert all(e.origin is Origin.INGESTED for e in entries)
    assert all(e.project == "remem" for e in entries)


def test_every_chunk_carries_its_src_and_sec_tags(store, owner, doc):
    ingest.ingest_file(store, owner.id, doc, project="remem")

    entries = {h.entry.title: h.entry for h in _live(store, owner, doc)}
    alpha = entries["design § Alpha"]
    assert ingest.src_tag(doc) in alpha.tags
    assert ingest.sec_tag("alpha") in alpha.tags
    anchor = entries["design"]
    assert ingest.src_tag(doc) in anchor.tags
    assert not [t for t in anchor.tags if t.startswith("sec:")]


def test_archive_writes_the_archived_origin(store, owner, doc):
    ingest.ingest_file(store, owner.id, doc, project="remem", archive=True)

    assert all(h.entry.origin is Origin.ARCHIVED for h in _live(store, owner, doc))


def test_re_ingesting_an_unchanged_file_writes_nothing(store, owner, doc):
    ingest.ingest_file(store, owner.id, doc, project="remem")
    before = {h.entry.id: h.entry.updated_at for h in _live(store, owner, doc)}

    report = ingest.ingest_file(store, owner.id, doc, project="remem")

    assert (report.created, report.changed, report.unchanged) == (0, 0, 3)
    after = {h.entry.id: h.entry.updated_at for h in _live(store, owner, doc)}
    assert after == before


def test_an_edited_section_supersedes_only_itself(store, owner, doc):
    ingest.ingest_file(store, owner.id, doc, project="remem")
    doc.write_text(DOC.replace("body a", "body a, revised"))

    report = ingest.ingest_file(store, owner.id, doc, project="remem")

    assert (report.created, report.changed, report.unchanged) == (0, 1, 2)
    bodies = {h.entry.title: h.entry.body for h in _live(store, owner, doc)}
    assert "body a, revised" in bodies["design § Alpha"]
    # The superseded original is kept, not destroyed.
    all_versions = store.search(
        Query(tags=[ingest.sec_tag("alpha")], include_superseded=True,
              origins=[Origin.INGESTED], limit=10),
        owner.id,
    )
    assert len(all_versions) == 2


def test_a_new_section_is_created_without_touching_its_neighbours(store, owner, doc):
    ingest.ingest_file(store, owner.id, doc, project="remem")
    doc.write_text(DOC + "\n## Gamma\n\nbody g\n")

    report = ingest.ingest_file(store, owner.id, doc, project="remem")

    assert (report.created, report.changed, report.unchanged) == (1, 0, 3)


def test_dry_run_reports_the_plan_and_writes_nothing(store, owner, doc):
    report = ingest.ingest_file(store, owner.id, doc, project="remem", dry_run=True)

    assert report.created == 3
    assert _live(store, owner, doc) == []


def test_too_many_chunks_raises_rather_than_truncating(store, owner, tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_CHUNKS_PER_FILE", 3)
    path = tmp_path / "big.md"
    path.write_text("# Big\n\nlead\n\n" + "".join(f"## S{i}\n\nb{i}\n\n" for i in range(5)))

    with pytest.raises(ingest.TooManyChunks):
        ingest.ingest_file(store, owner.id, path, project="remem")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ingest_service.py -v`
Expected: FAIL with `ImportError: cannot import name 'ingest'`.

- [ ] **Step 3: Write the service**

`src/remem/services/ingest.py`:

```python
"""Loading markdown documents into remem as chunk-sized entries.

Every policy decision about ingest lives here: what a chunk's identity is,
when a chunk is rewritten rather than left alone, and which origin it gets.
The CLI passes paths and prints counts.

Identity is a pair of tags, `src:<path>` and `sec:<slug>`, following the
handoff precedent - a tag convention plus supersede, no new table. There is
no content hash: to know whether a chunk changed, compare its body to the
stored one. A hash would need somewhere to live and would answer the same
question, less directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from remem.domain import Kind, Origin, Query
from remem.markdown import Chunk, split
from remem.services.write import remember, supersede
from remem.store import Store

#: The sweep lists a file's live chunks through `Query.limit`. Sweeping a
#: silently truncated list would supersede live chunks at random, which is
#: the worst failure available here - so exceeding this raises instead.
MAX_CHUNKS_PER_FILE = 200


class TooManyChunks(Exception):
    """A document produced more chunks than one sweep can safely list."""


@dataclass(slots=True)
class Report:
    created: int = 0
    changed: int = 0
    unchanged: int = 0
    swept: int = 0
    failures: list[tuple[Path, str]] = field(default_factory=list)

    def merge(self, other: Report) -> None:
        self.created += other.created
        self.changed += other.changed
        self.unchanged += other.unchanged
        self.swept += other.swept
        self.failures.extend(other.failures)


def src_tag(path: Path) -> str:
    """The tag naming a chunk's source file.

    Posix-normalised so a Windows ingest and a macOS one agree about
    identity, which is what makes re-ingest work across machines.
    """
    return f"src:{Path(path).as_posix()}"


def sec_tag(slug: str) -> str:
    return f"sec:{slug}"


def _live_chunks(store: Store, owner_id: UUID, path: Path) -> list:
    hits = store.search(
        Query(
            tags=[src_tag(path)],
            origins=[Origin.INGESTED, Origin.ARCHIVED],
            limit=MAX_CHUNKS_PER_FILE,
        ),
        owner_id,
    )
    if len(hits) >= MAX_CHUNKS_PER_FILE:
        raise TooManyChunks(
            f"{path} has {len(hits)} or more live chunks, at or above the "
            f"limit of {MAX_CHUNKS_PER_FILE}. Sweeping a truncated list would "
            f"supersede live entries at random."
        )
    return [h.entry for h in hits]


def ingest_file(
    store: Store,
    owner_id: UUID,
    path: Path,
    *,
    project: str | None,
    archive: bool = False,
    dry_run: bool = False,
) -> Report:
    """Ingest one file. Task 4 adds the sweep on top of this."""
    path = Path(path)
    chunks = split(path.read_text(), doc_title=path.stem)
    if len(chunks) > MAX_CHUNKS_PER_FILE:
        raise TooManyChunks(
            f"{path} produced {len(chunks)} chunks, over the limit of "
            f"{MAX_CHUNKS_PER_FILE}."
        )

    origin = Origin.ARCHIVED if archive else Origin.INGESTED
    existing = {_sec_of(e): e for e in _live_chunks(store, owner_id, path)}
    report = Report()

    for chunk in chunks:
        current = existing.get(chunk.slug)
        if current is None:
            report.created += 1
            if not dry_run:
                remember(
                    store, owner_id,
                    title=chunk.title, body=chunk.body, kind=Kind.DOC,
                    project=project, origin=origin,
                    tags=_tags_for(path, chunk),
                )
        elif current.body == chunk.body:
            # Skip entirely rather than rewrite an identical row: an update
            # would churn updated_at and, worse, invalidate nothing while
            # making `remem embed` look like it has work to redo.
            report.unchanged += 1
        else:
            report.changed += 1
            if not dry_run:
                supersede(store, owner_id, current.id,
                          title=chunk.title, body=chunk.body)
    return report


def _tags_for(path: Path, chunk: Chunk) -> list[str]:
    """The anchor carries no `sec:` tag - that absence is what identifies it."""
    tags = [src_tag(path)]
    if not chunk.anchor:
        tags.append(sec_tag(chunk.slug))
    return tags


def _sec_of(entry) -> str:
    """An entry's slug, or "" for the anchor."""
    for tag in entry.tags:
        if tag.startswith("sec:"):
            return tag[len("sec:"):]
    return ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ingest_service.py -v`
Expected: PASS, 8 tests, zero skips.

Note that `supersede` copies the old entry's tags onto the replacement, so a
superseded chunk keeps its `src:`/`sec:` pair. That is what makes the next
re-ingest find the replacement rather than the original.

- [ ] **Step 5: Commit**

```bash
git add src/remem/services/ingest.py tests/test_ingest_service.py
git commit -m "feat: ingest service, write and diff"
```

---

### Task 4: The sweep

**Files:**
- Modify: `src/remem/services/ingest.py`
- Test: `tests/test_ingest_sweep.py`

**Interfaces:**
- Consumes: everything Task 3 produced.
- Produces: `ingest.ingest_paths(store, owner_id, paths: list[Path], *, project, archive=False, dry_run=False) -> Report` - globs directories for `**/*.md`, ingests each file, collects per-file failures rather than aborting.

- [ ] **Step 1: Write the failing tests**

`tests/test_ingest_sweep.py`:

```python
from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Origin, Query
from remem.services import ingest

pytestmark = pytest.mark.db

DOC = "# Design\n\nlead matter\n\n## Alpha\n\nbody a\n\n## Beta\n\nbody b\n"


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


@pytest.fixture
def doc(tmp_path):
    path = tmp_path / "design.md"
    path.write_text(DOC)
    return path


def _titles(store, owner, path):
    hits = store.search(
        Query(tags=[ingest.src_tag(path)], origins=[Origin.INGESTED],
              limit=ingest.MAX_CHUNKS_PER_FILE),
        owner.id,
    )
    return {h.entry.title for h in hits}


def test_a_renamed_heading_leaves_no_live_orphan(store, owner, doc):
    ingest.ingest_file(store, owner.id, doc, project="remem")
    doc.write_text(DOC.replace("## Alpha", "## Alpha, revisited"))

    report = ingest.ingest_file(store, owner.id, doc, project="remem")

    assert report.created == 1
    assert report.swept == 1
    assert _titles(store, owner, doc) == {
        "design", "design § Alpha, revisited", "design § Beta",
    }


def test_a_swept_orphan_is_superseded_by_the_anchor(store, owner, doc):
    ingest.ingest_file(store, owner.id, doc, project="remem")
    anchor = next(
        h.entry for h in store.search(
            Query(tags=[ingest.src_tag(doc)], origins=[Origin.INGESTED], limit=10),
            owner.id)
        if h.entry.title == "design"
    )
    doc.write_text(DOC.replace("## Alpha\n\nbody a\n\n", ""))

    ingest.ingest_file(store, owner.id, doc, project="remem")

    orphans = store.search(
        Query(tags=[ingest.sec_tag("alpha")], include_superseded=True,
              origins=[Origin.INGESTED], limit=10),
        owner.id,
    )
    assert len(orphans) == 1
    assert orphans[0].entry.superseded_by == anchor.id


def test_the_sweep_does_not_reach_other_files(store, owner, tmp_path, doc):
    other = tmp_path / "other.md"
    other.write_text("# Other\n\nlead\n\n## Gamma\n\nbody g\n")
    ingest.ingest_file(store, owner.id, doc, project="remem")
    ingest.ingest_file(store, owner.id, other, project="remem")

    doc.write_text("# Design\n\nlead matter\n")
    ingest.ingest_file(store, owner.id, doc, project="remem")

    assert _titles(store, owner, other) == {"other", "other § Gamma"}


def test_dry_run_sweeps_nothing(store, owner, doc):
    ingest.ingest_file(store, owner.id, doc, project="remem")
    doc.write_text(DOC.replace("## Alpha\n\nbody a\n\n", ""))

    report = ingest.ingest_file(store, owner.id, doc, project="remem", dry_run=True)

    assert report.swept == 1
    assert "design § Alpha" in _titles(store, owner, doc)


def test_ingest_paths_globs_a_directory(store, owner, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.md").write_text("# A\n\nlead\n\n## One\n\nbody\n")
    (tmp_path / "sub" / "b.md").write_text("# B\n\nlead\n\n## Two\n\nbody\n")
    (tmp_path / "ignore.txt").write_text("not markdown")

    report = ingest.ingest_paths(store, owner.id, [tmp_path], project="remem")

    assert report.created == 4
    assert report.failures == []


def test_one_unreadable_file_does_not_cost_the_others(store, owner, tmp_path):
    (tmp_path / "good.md").write_text("# Good\n\nlead\n\n## One\n\nbody\n")
    (tmp_path / "bad.md").write_bytes(b"\xff\xfe\x00 not utf-8 \xff")

    report = ingest.ingest_paths(store, owner.id, [tmp_path], project="remem")

    assert report.created == 2
    assert len(report.failures) == 1
    assert report.failures[0][0].name == "bad.md"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ingest_sweep.py -v`
Expected: FAIL - `report.swept` is 0 and `ingest_paths` does not exist.

- [ ] **Step 3: Add the sweep to `ingest_file`**

In `src/remem/services/ingest.py`, inside `ingest_file`, track which slugs were seen and sweep the rest. Replace the loop's opening and add the sweep before `return report`:

```python
    origin = Origin.ARCHIVED if archive else Origin.INGESTED
    existing = {_sec_of(e): e for e in _live_chunks(store, owner_id, path)}
    report = Report()
    seen: set[str] = set()
    anchor_id = None

    for chunk in chunks:
        seen.add(chunk.slug)
        current = existing.get(chunk.slug)
        if current is None:
            report.created += 1
            if not dry_run:
                written = remember(
                    store, owner_id,
                    title=chunk.title, body=chunk.body, kind=Kind.DOC,
                    project=project, origin=origin,
                    tags=_tags_for(path, chunk),
                )
                if chunk.anchor:
                    anchor_id = written.id
        elif current.body == chunk.body:
            report.unchanged += 1
            if chunk.anchor:
                anchor_id = current.id
        else:
            report.changed += 1
            if not dry_run:
                replacement = supersede(
                    store, owner_id, current.id,
                    title=chunk.title, body=chunk.body,
                )
                if chunk.anchor:
                    # The CURRENT anchor, not the retired row: if the anchor
                    # was itself superseded this run, orphans must point at
                    # its replacement or they would point at a dead entry.
                    anchor_id = replacement.id

    # The sweep. A heading that was renamed or deleted leaves a live chunk
    # with no counterpart in this reading of the file; left alone it stays
    # live and silently stale, returning alongside its own replacement with
    # nothing to say which is current.
    #
    # Orphans are superseded BY THE ANCHOR because set_superseded requires a
    # replacement id and a deleted heading has none. "This section is gone,
    # the document is here" is the honest reading, and it costs no schema
    # change - the alternative was a retired_at column plus a new predicate
    # in every query the store runs, for one caller.
    for slug, entry in existing.items():
        if slug in seen:
            continue
        report.swept += 1
        if dry_run or anchor_id is None:
            continue
        # store.set_superseded directly, NOT write.supersede: supersede
        # creates a replacement entry, and a swept chunk has no replacement -
        # that absence is the whole reason the anchor exists. Calling it here
        # would duplicate the orphan instead of retiring it.
        store.set_superseded(entry.id, anchor_id, owner_id)

    return report
```

- [ ] **Step 4: Add `ingest_paths`**

Append to `src/remem/services/ingest.py`:

```python
def ingest_paths(
    store: Store,
    owner_id: UUID,
    paths: list[Path],
    *,
    project: str | None,
    archive: bool = False,
    dry_run: bool = False,
) -> Report:
    """Ingest files and directories, collecting failures rather than aborting.

    One bad encoding in a directory must not cost every other file in it -
    this is a bulk command, and a caller who gets nothing back for one
    unreadable file learns less than one who gets 18 ingests and a named
    failure. The CLI exits non-zero when `failures` is non-empty.
    """
    report = Report()
    for path in _discover(paths):
        try:
            report.merge(
                ingest_file(store, owner_id, path, project=project,
                            archive=archive, dry_run=dry_run)
            )
        except (OSError, UnicodeDecodeError, TooManyChunks) as exc:
            report.failures.append((path, str(exc)))
    return report


def _discover(paths: list[Path]) -> list[Path]:
    """Files as given, directories globbed for **/*.md, sorted for a stable
    report. Sorted matters: a dry run the user reads and then re-runs for
    real must list its files in the same order both times."""
    found: list[Path] = []
    for path in paths:
        path = Path(path)
        found.extend(sorted(path.rglob("*.md")) if path.is_dir() else [path])
    return found
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_ingest_sweep.py tests/test_ingest_service.py -v`
Expected: PASS, 14 tests total, zero skips.

- [ ] **Step 6: Commit**

```bash
git add src/remem/services/ingest.py tests/test_ingest_sweep.py
git commit -m "feat: sweep orphaned chunks on re-ingest"
```

---

### Task 5: The CLI

**Files:**
- Modify: `src/remem/cli.py` (new `ingest` command; `--archived` on `search`)
- Test: `tests/test_ingest_cli.py`

**Interfaces:**
- Consumes: `ingest.ingest_paths`, `ingest.Report`; `search.find(..., include_archived=...)`.
- Produces: `remem ingest <path>... [--archive] [--project X | --global] [--dry-run]`; `remem search --archived`.

- [ ] **Step 1: Write the failing tests**

`tests/test_ingest_cli.py`:

```python
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from remem.cli import app

pytestmark = pytest.mark.db

runner = CliRunner()


@pytest.fixture
def docs(tmp_path):
    (tmp_path / "a.md").write_text("# Alpha doc\n\nlead\n\n## One\n\nbody one\n")
    return tmp_path


def test_ingest_reports_counts(live_dsn, monkeypatch, docs):
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    result = runner.invoke(app, ["ingest", str(docs), "--project", "remem"])

    assert result.exit_code == 0
    assert "2 new" in result.stdout


def test_dry_run_says_so_and_writes_nothing(live_dsn, monkeypatch, docs):
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    runner.invoke(app, ["ingest", str(docs), "--project", "remem", "--dry-run"])

    result = runner.invoke(app, ["search", "body one", "--project", "remem"])
    assert "No matches." in result.stdout


def test_archived_chunks_are_hidden_until_asked_for(live_dsn, monkeypatch, docs):
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    runner.invoke(app, ["ingest", str(docs), "--project", "remem", "--archive"])

    hidden = runner.invoke(app, ["search", "body one", "--project", "remem"])
    assert "No matches." in hidden.stdout

    shown = runner.invoke(app, ["search", "body one", "--project", "remem", "--archived"])
    assert "Alpha doc" in shown.stdout


def test_a_failure_is_named_and_exits_non_zero(live_dsn, monkeypatch, docs):
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    (docs / "bad.md").write_bytes(b"\xff\xfe\x00 not utf-8 \xff")

    result = runner.invoke(app, ["ingest", str(docs), "--project", "remem"])

    assert result.exit_code == 1
    assert "bad.md" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ingest_cli.py -v`
Expected: FAIL - `No such command 'ingest'`.

- [ ] **Step 3: Add the command**

In `src/remem/cli.py`, add `from pathlib import Path` to the imports if absent, `from remem.services import ingest as ingest_service`, and the command:

```python
@app.command()
def ingest(
    paths: Annotated[list[Path], typer.Argument(help="Files or directories.")],
    archive: Annotated[bool, typer.Option("--archive")] = False,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    is_global: Annotated[bool, typer.Option("--global")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
):
    """Load markdown documents in as searchable, heading-sized entries.

    Re-ingesting is safe and cheap: unchanged sections are skipped entirely,
    edited ones supersede their previous version, and sections that have
    disappeared from the file are superseded by the document's anchor entry
    so nothing is left live and stale.

    --archive stores these documents under the 'archived' origin, which is
    excluded from default search results and reachable with
    `remem search --archived`. Use it for material that is history rather
    than reference - executed implementation plans, for instance.
    """
    resolved = _resolve_project(project, is_global)
    with _session() as s:
        report = ingest_service.ingest_paths(
            s.store, s.owner.id, list(paths),
            project=resolved, archive=archive, dry_run=dry_run,
        )
    prefix = "Would write: " if dry_run else ""
    typer.echo(
        f"{prefix}{report.created} new, {report.changed} changed, "
        f"{report.unchanged} unchanged, {report.swept} swept."
    )
    for path, reason in report.failures:
        typer.echo(f"failed: {path}: {reason}", err=True)
    if report.failures:
        # Fail-loud, unlike every hook in this repo: a person typed this.
        raise typer.Exit(1)
    if report.created or report.changed:
        typer.echo("Run `remem embed` to give the new entries vectors.")
```

- [ ] **Step 4: Add `--archived` to search**

In the `search` command signature, after the `handoff` option:

```python
    archived: Annotated[bool, typer.Option("--archived")] = False,
```

In the `find(...)` call, after `include_handoffs=handoff,`:

```python
        include_archived=archived,
```

And add to the docstring, after the `--handoff` line:

```
    --archived also searches archived document chunks, which are excluded
    by default.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_ingest_cli.py tests/test_cli.py -v`
Expected: PASS, zero skips. `test_cli.py` must still pass - Step 4 changed a command it covers.

- [ ] **Step 6: Commit**

```bash
git add src/remem/cli.py tests/test_ingest_cli.py
git commit -m "feat: remem ingest and search --archived"
```

---

### Task 6: MCP recall, the real ingest, and the docs

**Files:**
- Modify: `src/remem/mcp_server.py:90-140` (the `recall` tool)
- Modify: `CLAUDE.md`
- Test: `tests/test_mcp_ingest.py`

**Interfaces:**
- Consumes: `search.find(..., include_archived=...)`.
- Produces: `recall(..., include_archived: bool = False)`.

- [ ] **Step 1: Write the failing test**

`tests/test_mcp_ingest.py`:

`recall_tool` opens its own session through `open_session()`, so this test
uses the `env` fixture pattern from `tests/test_mcp_server.py` and the
committing `live_dsn` database, not `conn`:

```python
from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate

pytestmark = pytest.mark.db


@pytest.fixture
def env(live_dsn, monkeypatch, tmp_path):
    import psycopg
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))
    return live_dsn


def test_recall_hides_archived_chunks_unless_asked(env, tmp_path):
    from remem.mcp_server import recall_tool
    from remem.services import ingest
    from remem.session import open_session

    doc = tmp_path / "plan.md"
    doc.write_text("# Plan\n\nlead\n\n## Task 9\n\nsession wiring and the CLI\n")
    with open_session() as s:
        ingest.ingest_file(s.store, s.owner.id, doc, project="remem", archive=True)

    assert recall_tool(query="session wiring") == []

    shown = recall_tool(query="session wiring", include_archived=True)
    assert [h["title"] for h in shown] == ["plan § Task 9"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_ingest.py -v`
Expected: FAIL - unexpected keyword `include_archived`.

- [ ] **Step 3: Add the parameter**

In `src/remem/mcp_server.py`, add `include_archived: bool = False` beside `include_handoffs` in the tool signature, pass it through to `find`, and extend the docstring in the same voice as the existing `include_handoffs` line:

```
    include_archived: archived document chunks - executed implementation
    plans - are excluded by default because they are three times the volume
    of the reasoning docs and mostly source code that now lives in the repo.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_ingest.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass. Check the skip count against a run before this branch - it must not have grown.

- [ ] **Step 6: Ingest the real corpus**

```bash
uv tool install --editable .
remem ingest docs/superpowers/specs docs/superpowers/notes docs/superpowers/decisions-2026-08-26.md docs/superpowers/decisions-2026-08-26-capture.md --dry-run
```

Expected: roughly 211 new, 0 changed, 0 unchanged, 0 swept. Then without `--dry-run`, then:

```bash
remem ingest docs/superpowers/plans --archive
remem embed
remem search 'entry_events foreign key'
```

Expected: a hit whose title is `2026-08-28-events-and-recall-design § ...`, not a whole file. Then re-run the first ingest and confirm it reports all unchanged and zero written - that is the skip path working on real data.

- [ ] **Step 7: Document it in CLAUDE.md**

Add a section after "### Handoffs":

```markdown
### Ingested documents

`remem ingest <path>` loads markdown in as one entry per `h1`-`h3` heading,
plus an anchor entry per file. Identity is two tags, `src:<path>` and
`sec:<slug>`, so re-ingest is idempotent: unchanged sections are skipped
without a write, edited ones supersede their previous version, and sections
that vanished from the file are superseded **by that file's anchor** -
`set_superseded` needs a replacement id and a deleted heading has none.

Splitting is on headings and only on headings. A size-based sub-splitter
would cut through fenced code, which is most of what a plan contains. The
only fence logic in `markdown.py` is a boolean for heading detection, so a
`#` comment inside a code block is not mistaken for a section.

Two origins, because `search.DEFAULT_ORIGINS` is an allowlist and an
exclude filter was deliberately declined: `INGESTED` (specs, notes,
decisions) is in that list, `ARCHIVED` (plans, written with `--archive`) is
not and needs `--archived`. Plans are the minority by count and three times
the volume, and their bulk is source code that now lives in `src/`.
`DEFAULT_ORIGINS` must gain any future origin or that origin silently
vanishes from search.

`markdown.py` is pure - no I/O, no store - so its tests carry no `db`
marker and run on CI.
```

- [ ] **Step 8: Commit**

```bash
git add src/remem/mcp_server.py tests/test_mcp_ingest.py CLAUDE.md
git commit -m "feat: recall --include-archived; document ingest"
```
