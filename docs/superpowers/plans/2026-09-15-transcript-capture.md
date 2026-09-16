# Transcript Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store Claude Code session transcripts in Postgres byte-exact, parsed into per-line rows, so extraction and every future training use can be re-run against raw material the database owns.

**Architecture:** Four tables - `transcripts` (immutable source, `bytea`), `transcript_lines` (derived, droppable, cascade), `transcript_paths` (per-project directory claims), `transcript_runs` (observability). One pure module does parsing and append classification. One service owns every policy decision. Five CLI commands and one new spawn point, following `bag memory refresh` / `bag reingest run` exactly.

**Tech Stack:** Python 3.14, psycopg 3, Postgres 18, Typer, pytest.

**Spec:** `docs/superpowers/specs/2026-09-15-transcript-capture-design.md` - read it first. The plan argues from it and does not repeat its reasoning.

## Global Constraints

- **Python 3.14.** `from __future__ import annotations` at the top of every module. `StrEnum` from `enum`, `uuid7` via `saddlebag.domain.new_id`.
- **Verification is `make check` only.** It runs `ruff format` -> `ruff check --fix` -> `pyrefly check` -> `pytest`, stopping at the first failure. Never hand-edit anything ruff fixes.
- **Never edit an applied migration.** Add a new numbered file. Latest applied is `019_imported_origin.sql`; this plan adds 020-023.
- **`clock_timestamp()`, never `now()`** in migrations. Tests run in one rolled-back transaction where `now()` gives every row an identical timestamp.
- **Nothing outside `session.py` / `backends/` imports psycopg.**
- **All SQL text goes through `backends/postgres/sqltext.as_sql()`** when built with an f-string, and every interpolation must be a module constant. Every value is a parameter.
- **No `print()`.** `T20` is an error project-wide because the MCP server speaks JSON-RPC on stdout. CLI output goes through `typer.echo`; hook diagnostics through `hookio.debug` to stderr.
- **Prose convention: spaced hyphens ` - `, never em dashes**, in comments, docstrings and docs.
- **Comments explain *why*, at length**, especially where a decision looks arbitrary. Match the density of the surrounding code.
- **Line width 88, and the formatter owns it.** Do not reflow prose comments.
- **DB-backed tests are `@pytest.mark.db`.** A green run does not mean they ran - check the skip count.

---

### Task 1: Migrations and domain types

**Files:**
- Create: `src/saddlebag/backends/postgres/migrations/020_transcripts.sql`
- Create: `src/saddlebag/backends/postgres/migrations/021_transcript_lines.sql`
- Create: `src/saddlebag/backends/postgres/migrations/022_transcript_paths.sql`
- Create: `src/saddlebag/backends/postgres/migrations/023_transcript_runs.sql`
- Modify: `src/saddlebag/domain.py` (append new dataclasses and enum)
- Test: `tests/test_migrate.py` (add one test), `tests/test_domain.py` (add one test)

**Interfaces:**
- Consumes: nothing.
- Produces: tables `transcripts`, `transcript_lines`, `transcript_paths`, `transcript_runs`; `domain.Transcript`, `domain.TranscriptLine`, `domain.TranscriptPath`, `domain.TranscriptRun`, `domain.TranscriptTrigger`.

- [ ] **Step 1: Write the failing migration test**

Add to `tests/test_migrate.py`:

```python
@pytest.mark.db
def test_transcript_tables_exist_after_migrate(conn: psycopg.Connection[Any]) -> None:
    migrate(conn)
    for table in (
        "transcripts",
        "transcript_lines",
        "transcript_paths",
        "transcript_runs",
    ):
        assert scalar(conn.execute(f"select to_regclass('public.{table}')")) is not None


@pytest.mark.db
def test_transcript_lines_cascade_when_their_transcript_goes(
    conn: psycopg.Connection[Any],
) -> None:
    """The derived table must never outlive its source.

    Nothing may store anything only in transcript_lines, and the cascade is
    what makes that enforceable rather than merely intended.
    """
    migrate(conn)
    owner = uuid.uuid4()
    conn.execute(
        "insert into principals (id, kind, name) values (%s, 'human', 'test')",
        (owner,),
    )
    tid = uuid.uuid4()
    conn.execute(
        "insert into transcripts"
        " (id, owner_id, project, harness, session_id, path, content, bytes, sha256)"
        " values (%s, %s, 'p', 'claude-code', 's', '/tmp/s.jsonl', %s, 2, 'abc')",
        (tid, owner, b"{}"),
    )
    conn.execute(
        "insert into transcript_lines (transcript_id, seq, raw)"
        " values (%s, 0, '{}'::jsonb)",
        (tid,),
    )
    conn.execute("delete from transcripts where id = %s", (tid,))
    assert (
        scalar(conn.execute("select count(*) from transcript_lines")) == 0
    )
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_migrate.py -k transcript -v`
Expected: FAIL - `to_regclass` returns None, the table does not exist.

- [ ] **Step 3: Write migration 020**

`src/saddlebag/backends/postgres/migrations/020_transcripts.sql`:

```sql
-- The raw session transcript, byte for byte, as the source everything else
-- is derived from. The events pipeline records the tool layer and nothing
-- else - claude-code has thousands of tool_call rows and zero message rows -
-- so the prompts, the assistant text and the reasoning between the calls
-- have never been in the database at all. The full trace was always on disk
-- as JSONL, and every session_end payload already carries its path.
--
-- This finishes the decision the events design started rather than reversing
-- it: "derived data lives apart from its source, is recomputable, and never
-- overwrites it". Events fixed recomputability for the tool layer; this fixes
-- it for the rest.
--
-- Kept indefinitely, like events. Nothing prunes a row on a schedule.
create table transcripts (
  id uuid primary key,
  owner_id uuid not null references principals(id),
  project text not null,
  -- Not a promise. Claude Code is the only harness that writes a transcript
  -- today; the column exists so a second one needs no migration, and the
  -- table is simply empty for cursor and opencode. Nothing downstream may
  -- require a transcript to exist.
  harness text not null,
  session_id text not null,
  path text not null,
  -- bytea, not text. A transcript is a file another tool owns, and if one
  -- line is ever invalid UTF-8 then `text` refuses the insert and the whole
  -- session is lost rather than the line. The entire point of this row is
  -- that it survives a parser that does not.
  content bytea not null,
  bytes bigint not null,
  -- sha256 of `content`. The append check reads the first `bytes` bytes of
  -- the file on disk and compares: a match proves the file was appended to
  -- rather than rewritten, which is what lets an import add lines instead of
  -- rebuilding them.
  sha256 text not null,
  first_seen timestamptz not null default clock_timestamp(),
  last_read timestamptz not null default clock_timestamp(),
  -- Session id, not path, is identity. Two claimed directories hold
  -- different sessions, so path cannot be the key - but the same session
  -- must never land twice if two projects claim overlapping directories.
  -- This is also already how `events` identifies a session.
  unique (owner_id, harness, session_id)
);

create index transcripts_project_idx on transcripts (owner_id, project);
```

- [ ] **Step 4: Write migration 021**

`src/saddlebag/backends/postgres/migrations/021_transcript_lines.sql`:

```sql
-- Derived from transcripts.content, and explicitly droppable: this table can
-- be deleted and rebuilt from the source at any time, which is what makes a
-- parser bug recoverable instead of fatal.
--
-- The rule that follows from that, and the one thing most likely to be
-- broken later: NOTHING MAY STORE ANYTHING ONLY HERE. Labels, chunks and
-- training signals reference (transcript_id, seq) from their own tables. The
-- moment a label lives in this table, rebuilding the parse destroys training
-- data and "derived lives apart from its source" inverts.
--
-- (transcript_id, seq) is a stable coordinate because JSONL line numbers do
-- not shift under append - which is precisely why the import refuses to
-- follow a file that shrank.
create table transcript_lines (
  transcript_id uuid not null references transcripts(id) on delete cascade,
  -- 0-based line number within the file.
  seq int not null,
  -- `type` and `occurred_at` are conveniences hoisted out of `raw` for
  -- indexing, and both are nullable on purpose. Claude Code's format is not
  -- ours and will change; a line whose shape is unrecognised still stores,
  -- with nulls, rather than failing the import. `raw` is always complete.
  type text,
  uuid text,
  occurred_at timestamptz,
  raw jsonb not null,
  primary key (transcript_id, seq)
);

create index transcript_lines_type_idx on transcript_lines (transcript_id, type);
```

- [ ] **Step 5: Write migration 022**

`src/saddlebag/backends/postgres/migrations/022_transcript_paths.sql`:

```sql
-- Which transcript directories a project claims.
--
-- Recording is opt-in per project and that gate governs recording going
-- FORWARD. A claim here is the separate, deliberate opt-in for BACKFILL,
-- because claiming a directory imports all of it - including sessions that
-- predate the recording pipeline entirely. Nothing auto-claims; `discover`
-- proposes and writes nothing.
--
-- Paths are stored ABSOLUTE, as given. This is deliberately unlike
-- ingest_designations, which stores repo-relative paths and resolves them
-- against the git root: these directories live outside any repository and
-- there is no root to resolve against.
create table transcript_paths (
  owner_id uuid not null references principals(id),
  project text not null,
  path text not null,
  added_at timestamptz not null default clock_timestamp(),
  primary key (owner_id, project, path)
);

-- A directory belongs to at most one project. Two projects claiming the same
-- directory would file the same session twice under different projects, and
-- the unique constraint on transcripts would then reject the second import
-- with nothing explaining why.
create unique index transcript_paths_one_owner_idx on transcript_paths (owner_id, path);
```

- [ ] **Step 6: Write migration 023**

`src/saddlebag/backends/postgres/migrations/023_transcript_runs.sql`:

```sql
-- One row per import invocation, typed or spawned alike, written by the
-- service so both record identically - the spawned one being the caller with
-- no terminal, and the reason this table exists before it does.
--
-- A started row with `finished_at` null is a statement, not a gap: the
-- process died between starting and finishing, which is what makes "crashed"
-- distinguishable from "never ran". It only works because both commands open
-- with autocommit and commit the started row before reading any file.
create table transcript_runs (
  id uuid primary key,
  owner_id uuid not null references principals(id),
  project text not null,
  -- 'auto' for the spawned refresh, 'manual' for `bag transcripts import`.
  -- Text with a check rather than an enum: two values, and the check reads
  -- the same. A reader cannot otherwise tell an unattended run from their
  -- own, and believing the automatic half ran when only a manual one had is
  -- the false premise `status` names the trigger to prevent.
  trigger text not null check (trigger in ('auto', 'manual')),
  started_at timestamptz not null default clock_timestamp(),
  finished_at timestamptz,
  files_seen int not null default 0,
  files_new int not null default 0,
  files_appended int not null default 0,
  files_rebuilt int not null default 0,
  lines_written bigint not null default 0,
  bytes_written bigint not null default 0,
  -- [{"path": ..., "stored": n, "on_disk": n}] - files that shrank. The
  -- import refuses to follow these: the stored copy is more complete than
  -- what is on disk, and the purpose of the source row is that a rotating
  -- file does not destroy the session.
  anomalies jsonb not null default '[]'::jsonb,
  -- [{"path": ..., "reason": ...}]. A line that is not valid JSON lands here
  -- too, with its seq in the reason, so no line is ever silently dropped.
  failures jsonb not null default '[]'::jsonb
);

create index transcript_runs_latest_idx
  on transcript_runs (owner_id, project, started_at desc);
```

- [ ] **Step 7: Run the migration test to verify it passes**

Run: `uv run pytest tests/test_migrate.py -k transcript -v`
Expected: PASS, 2 tests.

- [ ] **Step 8: Write the failing domain test**

Add to `tests/test_domain.py`:

```python
def test_transcript_trigger_values_match_the_migration_check() -> None:
    """The check constraint in 023 allows exactly these two strings."""
    assert {str(t) for t in TranscriptTrigger} == {"auto", "manual"}


def test_transcript_does_not_carry_its_content() -> None:
    """Listing transcripts must never drag 156MB through memory.

    `content` is fetched deliberately, by its own store call, and only when
    something is about to parse it. A field here would make every list
    operation pay for every byte.
    """
    assert "content" not in {f.name for f in fields(Transcript)}
```

- [ ] **Step 9: Run to verify it fails**

Run: `uv run pytest tests/test_domain.py -k transcript -v`
Expected: FAIL with `ImportError: cannot import name 'TranscriptTrigger'`.

- [ ] **Step 10: Add the domain types**

Append to `src/saddlebag/domain.py` (and add every new name to `__all__`):

```python
class TranscriptTrigger(StrEnum):
    """Who started an import. See transcript_runs.trigger."""

    AUTO = "auto"
    MANUAL = "manual"


@dataclass
class Transcript:
    """A stored session transcript, without its bytes.

    `content` is deliberately absent: a transcript is megabytes, listing them
    is common, and reading the bytes is a separate store call made only when
    something is about to parse them.
    """

    id: UUID
    owner_id: UUID
    project: str
    harness: str
    session_id: str
    path: str
    bytes: int
    sha256: str
    first_seen: datetime
    last_read: datetime


@dataclass
class TranscriptLine:
    """One parsed JSONL line. Derived, and rebuildable from the source."""

    seq: int
    type: str | None
    uuid: str | None
    occurred_at: datetime | None
    raw: dict[str, Any]


@dataclass
class TranscriptPath:
    """A directory a project claims. Absolute, as the user gave it."""

    owner_id: UUID
    project: str
    path: str
    added_at: datetime


@dataclass
class TranscriptRun:
    """One import invocation. `finished_at` None means it died mid-run."""

    id: UUID
    owner_id: UUID
    project: str
    trigger: TranscriptTrigger
    started_at: datetime
    finished_at: datetime | None
    files_seen: int
    files_new: int
    files_appended: int
    files_rebuilt: int
    lines_written: int
    bytes_written: int
    anomalies: list[dict[str, Any]]
    failures: list[dict[str, Any]]
```

- [ ] **Step 11: Run both test files**

Run: `uv run pytest tests/test_domain.py tests/test_migrate.py -k transcript -v`
Expected: PASS, 4 tests.

- [ ] **Step 12: Run the gate**

Run: `make check`
Expected: exit 0. If pyrefly complains about `Any` in a signature, import it from `typing` - the strict preset requires annotation completeness.

- [ ] **Step 13: Commit**

```bash
git add src/saddlebag/backends/postgres/migrations/02*.sql src/saddlebag/domain.py tests/test_domain.py tests/test_migrate.py
git commit -m "Add transcript tables and domain types

Four tables: transcripts (byte-exact source), transcript_lines (derived
and droppable), transcript_paths (per-project claims), transcript_runs
(observability). The cascade on transcript_lines is what makes 'nothing
may store anything only here' enforceable rather than intended."
```

---

### Task 2: The pure parsing module

**Files:**
- Create: `src/saddlebag/transcript_file.py`
- Test: `tests/test_transcript_file.py`

**Interfaces:**
- Consumes: `domain.TranscriptLine`.
- Produces: `ReadPlan` (StrEnum: `SKIP`/`APPEND`/`REBUILD`/`SHRUNK`), `classify(stored_bytes: int, stored_sha: str, disk_size: int, disk_prefix_sha: str | None) -> ReadPlan`, `sha256_hex(data: bytes) -> str`, `LineFailure` (dataclass: `seq: int`, `reason: str`), `parse(content: bytes, start_seq: int = 0) -> tuple[list[TranscriptLine], list[LineFailure]]`.

This module is **pure** - no I/O, no store, no filesystem - so its tests carry no `db` marker and run on CI. Same shape as `markdown.py`, `memory_file.py` and `session_size.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_transcript_file.py`:

```python
"""The pure half of transcript capture: parsing and append classification.

No `db` marker anywhere in this file, deliberately - nothing here touches
Postgres or the filesystem, so every one of these runs on CI. The properties
tested are the ones that actually break, chosen the way memory_file's were.
"""

from __future__ import annotations

import json

from saddlebag.transcript_file import (
    ReadPlan,
    classify,
    parse,
    sha256_hex,
)


def test_classify_skips_a_file_that_has_not_grown() -> None:
    """The common case at every session start, and it costs one stat."""
    assert classify(100, "abc", 100, None) is ReadPlan.SKIP


def test_classify_appends_when_the_prefix_still_matches() -> None:
    assert classify(100, "abc", 180, "abc") is ReadPlan.APPEND


def test_classify_rebuilds_when_the_prefix_changed() -> None:
    """The file was rewritten, not appended. Safe: lines are derived."""
    assert classify(100, "abc", 180, "def") is ReadPlan.REBUILD


def test_classify_reports_a_shrunk_file_rather_than_following_it() -> None:
    """The one case where the file on disk is NOT followed.

    Our stored copy is more complete, and the purpose of the source row is
    that a rotating file does not destroy the session.
    """
    assert classify(100, "abc", 40, "abc") is ReadPlan.SHRUNK


def test_sha256_hex_is_stable_and_hex() -> None:
    got = sha256_hex(b"hello")
    assert got == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_parse_accounts_for_every_line() -> None:
    """Rows plus failures equals lines. Nothing is ever silently dropped.

    A line whose SHAPE is unrecognised becomes a row with a null type; a line
    that is not valid JSON cannot become a jsonb row at all and is named as a
    failure instead. The conservation is the property.
    """
    content = b"\n".join(
        [
            json.dumps({"type": "user", "uuid": "u1"}).encode(),
            b"this is not json",
            json.dumps({"no_type_field": True}).encode(),
        ]
    )
    lines, failures = parse(content)
    assert len(lines) + len(failures) == 3
    assert [line.seq for line in lines] == [0, 2]
    assert [f.seq for f in failures] == [1]
    assert lines[1].type is None


def test_parse_keeps_the_whole_line_in_raw() -> None:
    payload = {"type": "assistant", "uuid": "x", "message": {"deep": [1, 2]}}
    lines, _ = parse(json.dumps(payload).encode())
    assert lines[0].raw == payload


def test_parse_reads_the_timestamp_when_there_is_one() -> None:
    content = json.dumps(
        {"type": "user", "timestamp": "2026-09-15T12:00:00.000Z"}
    ).encode()
    lines, _ = parse(content)
    assert lines[0].occurred_at is not None
    assert lines[0].occurred_at.year == 2026


def test_parse_tolerates_an_unparseable_timestamp() -> None:
    """A field we hoist for indexing must never fail an import."""
    content = json.dumps({"type": "user", "timestamp": "last tuesday"}).encode()
    lines, failures = parse(content)
    assert failures == []
    assert lines[0].occurred_at is None


def test_parse_ignores_blank_lines_without_counting_them() -> None:
    """A trailing newline is not a line, and must not become a null row."""
    content = b'{"type": "user"}\n\n'
    lines, failures = parse(content)
    assert len(lines) == 1
    assert failures == []


def test_parse_survives_invalid_utf8() -> None:
    """The reason `content` is bytea. One bad byte must cost one line."""
    content = b'{"type": "user"}\n' + b"\xff\xfe not utf-8\n"
    lines, failures = parse(content)
    assert len(lines) == 1
    assert len(failures) == 1


def test_parse_starts_at_the_offset_it_is_given() -> None:
    """Append parses only the tail, and the seq must continue the file."""
    lines, _ = parse(b'{"type": "user"}', start_seq: int = 7)  # noqa: E999
    assert lines[0].seq == 7


def test_parse_is_idempotent() -> None:
    """Rebuilding the derived table must always produce the same rows.

    Everything downstream rests on this: a label keyed on (transcript_id,
    seq) is only safe if a re-parse puts the same content at the same seq.
    """
    content = b'{"type": "user"}\n{"type": "assistant"}\n'
    first, _ = parse(content)
    second, _ = parse(content)
    assert [(line.seq, line.raw) for line in first] == [
        (line.seq, line.raw) for line in second
    ]
```

Note: the `start_seq` test above is written with a deliberate syntax error to
be fixed in step 3 - write it as `parse(b'{"type": "user"}', start_seq=7)`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_transcript_file.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'saddlebag.transcript_file'`.

- [ ] **Step 3: Write the module**

Create `src/saddlebag/transcript_file.py`:

```python
"""Parsing a Claude Code transcript, and deciding how to re-read one.

Pure: no I/O, no store, no filesystem. That is what lets this file's tests
run on CI, where there is no Postgres and no `~/.claude` - the same bargain
`markdown.py`, `memory_file.py` and `session_size.py` make.

The caller does every read; this module is handed bytes and sizes and
returns decisions and rows.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from saddlebag.domain import TranscriptLine

__all__ = [
    "LineFailure",
    "ReadPlan",
    "classify",
    "parse",
    "sha256_hex",
]


class ReadPlan(StrEnum):
    """What an import should do with a file it has seen before."""

    SKIP = "skip"
    APPEND = "append"
    REBUILD = "rebuild"
    SHRUNK = "shrunk"


@dataclass
class LineFailure:
    """A line that could not become a row, named rather than dropped."""

    seq: int
    reason: str


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def classify(
    stored_bytes: int,
    stored_sha: str,
    disk_size: int,
    disk_prefix_sha: str | None,
) -> ReadPlan:
    """Decide how to re-read a transcript, from sizes and hashes alone.

    Pure so that all four branches are testable without a filesystem, and
    ordered so the cheap answer comes first: an unchanged size is the common
    case at every session start, and it must cost one stat and no read. The
    caller only computes `disk_prefix_sha` - which needs reading
    `stored_bytes` bytes - when the size grew.

    A file that SHRANK is the one case where the file on disk is not
    followed. The stored copy is more complete, and two sessions are already
    gone from disk: the purpose of the source row is that a rotating file
    does not destroy the session.
    """
    if disk_size == stored_bytes:
        return ReadPlan.SKIP
    if disk_size < stored_bytes:
        return ReadPlan.SHRUNK
    if disk_prefix_sha is not None and disk_prefix_sha == stored_sha:
        return ReadPlan.APPEND
    # The prefix changed, so the file was rewritten rather than appended to.
    # Cheap to recover from and safe by construction: the lines are derived
    # and hold nothing of their own.
    return ReadPlan.REBUILD


def parse(
    content: bytes, start_seq: int = 0
) -> tuple[list[TranscriptLine], list[LineFailure]]:
    """Split JSONL into rows, accounting for every line.

    The invariant, and the one the test asserts: rows written plus failures
    recorded equals the number of non-empty lines. A line whose SHAPE is
    unrecognised becomes a row with a null `type` - Claude Code's format is
    not ours and will change, and an import that fails on an unfamiliar line
    would strand a whole session. A line that is not valid JSON cannot become
    a `jsonb` row at all, so it is named as a failure instead. Neither is
    ever silently dropped.

    `start_seq` exists for the append path, which parses only the tail: seq
    is the line's number within the FILE, not within this call, because
    (transcript_id, seq) is the coordinate labels will reference and it has
    to mean the same thing after an append as before one.

    Blank lines are not lines. A trailing newline must not become a null row.
    """
    lines: list[TranscriptLine] = []
    failures: list[LineFailure] = []

    for offset, raw_line in enumerate(content.split(b"\n")):
        if not raw_line.strip():
            continue
        seq = start_seq + offset
        try:
            # Decoding and parsing in one try: invalid UTF-8 and invalid JSON
            # are the same outcome here - a line that cannot be a jsonb row -
            # and splitting them would only give two names to one recovery.
            payload = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            failures.append(LineFailure(seq=seq, reason=f"line {seq}: {exc}"))
            continue
        if not isinstance(payload, dict):
            # A bare string or list is valid JSON and not a transcript line.
            # It stores as raw would be ambiguous, so it is a named failure.
            failures.append(
                LineFailure(seq=seq, reason=f"line {seq}: not a JSON object")
            )
            continue
        lines.append(
            TranscriptLine(
                seq=seq,
                type=_text(payload.get("type")),
                uuid=_text(payload.get("uuid")),
                occurred_at=_timestamp(payload.get("timestamp")),
                raw=payload,
            )
        )

    return lines, failures


def _text(value: Any) -> str | None:
    """A hoisted field, only when it really is text.

    A `type` that arrives as a number is a format change, not a crash: the
    value stays in `raw` and the hoisted column goes null.
    """
    return value if isinstance(value, str) else None


def _timestamp(value: Any) -> datetime | None:
    """Claude Code's ISO-8601 timestamp, or None.

    Nullable for the same reason `type` is: this column exists for indexing
    and nothing depends on it, so an unrecognised shape must cost the index
    entry and never the line. `Z` is spelled out because `fromisoformat`
    accepts it only from 3.11 onward and being explicit costs nothing.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_transcript_file.py -v`
Expected: PASS, 13 tests. Fix the deliberate syntax error in the `start_seq` test if you have not already.

- [ ] **Step 5: Run the gate**

Run: `make check`
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/saddlebag/transcript_file.py tests/test_transcript_file.py
git commit -m "Add the pure transcript parser and append classifier

Pure module, so its tests run on CI with no Postgres and no ~/.claude.
parse() conserves lines - rows plus failures equals lines, and neither an
unrecognised shape nor invalid JSON is ever silently dropped. classify()
orders its branches so an unchanged file costs one stat."
```

---

### Task 3: Store methods for transcripts and lines

**Files:**
- Modify: `src/saddlebag/store.py` (add to the Protocol)
- Modify: `src/saddlebag/backends/postgres/store.py`
- Test: `tests/test_store_transcripts.py` (create)

**Interfaces:**
- Consumes: `domain.Transcript`, `domain.TranscriptLine`, `transcript_file.LineFailure`.
- Produces on `Store`:
  - `put_transcript(owner_id: UUID, project: str, harness: str, session_id: str, path: str, content: bytes, sha256: str) -> Transcript`
  - `get_transcript(owner_id: UUID, harness: str, session_id: str) -> Transcript | None`
  - `transcript_content(transcript_id: UUID, owner_id: UUID) -> bytes | None`
  - `append_transcript(transcript_id: UUID, owner_id: UUID, tail: bytes, sha256: str) -> bool`
  - `replace_transcript_lines(transcript_id: UUID, lines: list[TranscriptLine]) -> int`
  - `add_transcript_lines(transcript_id: UUID, lines: list[TranscriptLine]) -> int`
  - `transcript_line_count(transcript_id: UUID) -> int`
  - `stored_transcripts(owner_id: UUID, project: str) -> list[Transcript]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_store_transcripts.py`:

```python
"""Store-level behaviour for transcripts. Every test here needs Postgres."""

from __future__ import annotations

import json

import pytest

from saddlebag.domain import TranscriptLine
from saddlebag.transcript_file import parse, sha256_hex

pytestmark = pytest.mark.db


def test_put_transcript_round_trips_content_byte_for_byte(store, owner) -> None:
    """The property the whole design rests on.

    Not an approximation of it: if the bytes come back different, the source
    row is worthless and every recovery path built on it is a lie.
    """
    content = b'{"type": "user"}\n' + b"\xff\xfe invalid utf-8\n"
    got = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/tmp/s1.jsonl", content,
        sha256_hex(content),
    )
    assert store.transcript_content(got.id, owner.id) == content


def test_put_transcript_is_idempotent_on_the_same_session(store, owner) -> None:
    """Re-importing a session updates it rather than creating a twin."""
    first = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/tmp/s1.jsonl", b"{}", "aaa"
    )
    second = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/tmp/s1.jsonl", b"{}{}", "bbb"
    )
    assert first.id == second.id
    assert second.bytes == 4
    assert second.sha256 == "bbb"


def test_append_transcript_adds_bytes_without_rewriting(store, owner) -> None:
    head = b'{"type": "user"}\n'
    tail = b'{"type": "assistant"}\n'
    t = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/tmp/s1.jsonl", head,
        sha256_hex(head),
    )
    assert store.append_transcript(
        t.id, owner.id, tail, sha256_hex(head + tail)
    ) is True
    assert store.transcript_content(t.id, owner.id) == head + tail


def test_append_transcript_refuses_another_owner(store, owner, other) -> None:
    """Ownership is enforced inside the store, never by callers."""
    t = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/tmp/s1.jsonl", b"{}", "aaa"
    )
    assert store.append_transcript(t.id, other.id, b"{}", "bbb") is False


def test_replace_transcript_lines_rebuilds_from_scratch(store, owner) -> None:
    t = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/tmp/s1.jsonl", b"{}", "aaa"
    )
    lines, _ = parse(b'{"type": "user"}\n{"type": "assistant"}\n')
    assert store.replace_transcript_lines(t.id, lines) == 2
    assert store.transcript_line_count(t.id) == 2
    # Rebuilding with fewer lines must leave no orphans from the first pass.
    fewer, _ = parse(b'{"type": "user"}\n')
    assert store.replace_transcript_lines(t.id, fewer) == 1
    assert store.transcript_line_count(t.id) == 1


def test_add_transcript_lines_continues_the_sequence(store, owner) -> None:
    t = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/tmp/s1.jsonl", b"{}", "aaa"
    )
    head, _ = parse(b'{"type": "user"}\n')
    store.replace_transcript_lines(t.id, head)
    tail, _ = parse(b'{"type": "assistant"}\n', start_seq=1)
    assert store.add_transcript_lines(t.id, tail) == 1
    assert store.transcript_line_count(t.id) == 2


def test_stored_transcripts_lists_without_content(store, owner) -> None:
    store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/tmp/s1.jsonl", b"{}" * 100, "aaa"
    )
    got = store.stored_transcripts(owner.id, "p")
    assert len(got) == 1
    assert not hasattr(got[0], "content")
```

`store`, `owner` and `other` are NOT in conftest - every store test file
defines its own, and they look like this (copy verbatim from
`tests/test_store_ingest_runs.py`):

```python
pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


@pytest.fixture
def other(store: PostgresStore) -> Principal:
    return store.ensure_principal("someone-else")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_store_transcripts.py -v`
Expected: FAIL - `AttributeError: 'PostgresStore' object has no attribute 'put_transcript'`.

- [ ] **Step 3: Add the Protocol declarations**

In `src/saddlebag/store.py`, after the collections block:

```python
    # transcripts
    #: Upsert by (owner, harness, session). Re-importing a session updates it
    #: rather than creating a twin - session id is identity, not path.
    def put_transcript(
        self,
        owner_id: UUID,
        project: str,
        harness: str,
        session_id: str,
        path: str,
        content: bytes,
        sha256: str,
    ) -> Transcript: ...
    def get_transcript(
        self, owner_id: UUID, harness: str, session_id: str
    ) -> Transcript | None: ...
    #: The bytes, fetched deliberately and separately. `Transcript` does not
    #: carry them: listing is common and a transcript is megabytes.
    def transcript_content(
        self, transcript_id: UUID, owner_id: UUID
    ) -> bytes | None: ...
    #: False when the ownership guard matched nothing - the same contract as
    #: `pin` and `set_superseded`, and callers must check it.
    def append_transcript(
        self, transcript_id: UUID, owner_id: UUID, tail: bytes, sha256: str
    ) -> bool: ...
    #: Delete and rewrite every line. Safe by construction: the rows are
    #: derived, and nothing may store anything only in them.
    def replace_transcript_lines(
        self, transcript_id: UUID, lines: list[TranscriptLine]
    ) -> int: ...
    def add_transcript_lines(
        self, transcript_id: UUID, lines: list[TranscriptLine]
    ) -> int: ...
    def transcript_line_count(self, transcript_id: UUID) -> int: ...
    def stored_transcripts(
        self, owner_id: UUID, project: str
    ) -> list[Transcript]: ...
```

- [ ] **Step 4: Implement in the Postgres backend**

In `src/saddlebag/backends/postgres/store.py`, add a module constant beside
the other column helpers and the methods below it:

```python
def transcript_columns(alias: str = "t") -> str:
    """The Transcript fields, in dataclass order. `content` is NOT here.

    A module constant so `as_sql` interpolation stays an interpolation of our
    own text, and so that listing transcripts can never accidentally select
    megabytes of bytea.
    """
    cols = (
        "id",
        "owner_id",
        "project",
        "harness",
        "session_id",
        "path",
        "bytes",
        "sha256",
        "first_seen",
        "last_read",
    )
    return ", ".join(f"{alias}.{c}" for c in cols)
```

```python
    def put_transcript(
        self,
        owner_id: UUID,
        project: str,
        harness: str,
        session_id: str,
        path: str,
        content: bytes,
        sha256: str,
    ) -> Transcript:
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                insert into transcripts
                  (id, owner_id, project, harness, session_id, path,
                   content, bytes, sha256)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (owner_id, harness, session_id) do update
                  set content = excluded.content,
                      bytes = excluded.bytes,
                      sha256 = excluded.sha256,
                      path = excluded.path,
                      project = excluded.project,
                      last_read = clock_timestamp()
                returning {transcript_columns("transcripts")}
                """),
                (
                    new_id(),
                    owner_id,
                    project,
                    harness,
                    session_id,
                    path,
                    content,
                    len(content),
                    sha256,
                ),
            )
            return _row_to_transcript(_one(cur))

    def get_transcript(
        self, owner_id: UUID, harness: str, session_id: str
    ) -> Transcript | None:
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select {transcript_columns("t")} from transcripts t
                 where t.owner_id = %s and t.harness = %s and t.session_id = %s
                """),
                (owner_id, harness, session_id),
            )
            row = cur.fetchone()
            return _row_to_transcript(row) if row else None

    def transcript_content(
        self, transcript_id: UUID, owner_id: UUID
    ) -> bytes | None:
        with self._cur() as cur:
            cur.execute(
                "select content from transcripts where id = %s and owner_id = %s",
                (transcript_id, owner_id),
            )
            row = cur.fetchone()
            return bytes(row[0]) if row else None

    def append_transcript(
        self, transcript_id: UUID, owner_id: UUID, tail: bytes, sha256: str
    ) -> bool:
        """Append bytes in place, in one statement.

        `content || %s` rather than read-modify-write: the bytes never travel
        to Python and back, which for a 22MB transcript is the difference
        between a cheap session-start refresh and an expensive one.
        """
        with self._cur() as cur:
            cur.execute(
                """
                update transcripts
                   set content = content || %s,
                       bytes = bytes + %s,
                       sha256 = %s,
                       last_read = clock_timestamp()
                 where id = %s and owner_id = %s
                """,
                (tail, len(tail), sha256, transcript_id, owner_id),
            )
            return cur.rowcount == 1

    def replace_transcript_lines(
        self, transcript_id: UUID, lines: list[TranscriptLine]
    ) -> int:
        with self._cur() as cur:
            cur.execute(
                "delete from transcript_lines where transcript_id = %s",
                (transcript_id,),
            )
        return self.add_transcript_lines(transcript_id, lines)

    def add_transcript_lines(
        self, transcript_id: UUID, lines: list[TranscriptLine]
    ) -> int:
        if not lines:
            return 0
        with self._cur() as cur:
            cur.executemany(
                """
                insert into transcript_lines
                  (transcript_id, seq, type, uuid, occurred_at, raw)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (transcript_id, seq) do nothing
                """,
                [
                    (
                        transcript_id,
                        line.seq,
                        line.type,
                        line.uuid,
                        line.occurred_at,
                        Jsonb(line.raw),
                    )
                    for line in lines
                ],
            )
        return len(lines)

    def transcript_line_count(self, transcript_id: UUID) -> int:
        with self._cur() as cur:
            cur.execute(
                "select count(*) from transcript_lines where transcript_id = %s",
                (transcript_id,),
            )
            return int(_one(cur)[0])

    def stored_transcripts(
        self, owner_id: UUID, project: str
    ) -> list[Transcript]:
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select {transcript_columns("t")} from transcripts t
                 where t.owner_id = %s and t.project = %s
                 order by t.first_seen
                """),
                (owner_id, project),
            )
            return [_row_to_transcript(r) for r in cur.fetchall()]
```

Add `_row_to_transcript` beside the other row mappers, following the shape of
`_row_to_entry`. Import `Jsonb` from `psycopg.types.json` if it is not already
imported in this module.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_store_transcripts.py -v`
Expected: PASS, 7 tests. If they skip, Postgres is not running - `docker compose up -d` and re-run, and check the skip count.

- [ ] **Step 6: Run the gate**

Run: `make check`
Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/saddlebag/store.py src/saddlebag/backends/postgres/store.py tests/test_store_transcripts.py
git commit -m "Add store methods for transcripts and their derived lines

append_transcript concatenates in SQL rather than reading the bytes back
into Python - for a 22MB transcript that is the difference between a
cheap session-start refresh and an expensive one. transcript_columns
deliberately omits content so listing can never select megabytes."
```

---

### Task 4: Store methods for paths and runs

**Files:**
- Modify: `src/saddlebag/store.py`, `src/saddlebag/backends/postgres/store.py`
- Test: `tests/test_store_transcripts.py` (extend)

**Interfaces:**
- Produces on `Store`:
  - `add_transcript_path(owner_id: UUID, project: str, path: str) -> str | None` - returns the *conflicting project* when the path is already claimed by another, else `None` for success.
  - `remove_transcript_path(owner_id: UUID, project: str, path: str) -> bool`
  - `transcript_paths(owner_id: UUID, project: str | None = None) -> list[TranscriptPath]`
  - `start_transcript_run(owner_id: UUID, project: str, trigger: TranscriptTrigger) -> TranscriptRun`
  - `finish_transcript_run(run_id: UUID, owner_id: UUID, *, files_seen: int, files_new: int, files_appended: int, files_rebuilt: int, lines_written: int, bytes_written: int, anomalies: list[dict[str, Any]], failures: list[dict[str, Any]]) -> TranscriptRun`
  - `latest_transcript_run(owner_id: UUID, project: str) -> TranscriptRun | None`
  - `event_session_ids(owner_id: UUID, project: str) -> list[str]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_store_transcripts.py`:

```python
def test_add_transcript_path_names_the_project_already_holding_it(
    store, owner
) -> None:
    """A directory belongs to at most one project, and the refusal says whose.

    Without the name, a user is told "taken" and has no way to find out by
    what - and the real failure only surfaces much later, as a session
    filed under the wrong project.
    """
    assert store.add_transcript_path(owner.id, "alpha", "/tmp/dir") is None
    assert store.add_transcript_path(owner.id, "beta", "/tmp/dir") == "alpha"


def test_add_transcript_path_is_idempotent_for_the_same_project(
    store, owner
) -> None:
    assert store.add_transcript_path(owner.id, "alpha", "/tmp/dir") is None
    assert store.add_transcript_path(owner.id, "alpha", "/tmp/dir") is None
    assert len(store.transcript_paths(owner.id, "alpha")) == 1


def test_transcript_paths_with_no_project_sweeps_every_claim(store, owner) -> None:
    store.add_transcript_path(owner.id, "alpha", "/tmp/a")
    store.add_transcript_path(owner.id, "beta", "/tmp/b")
    assert len(store.transcript_paths(owner.id)) == 2


def test_a_started_run_has_no_finished_at(store, owner) -> None:
    """The started row is what makes 'crashed' distinguishable from 'never
    ran', and it only works if it commits before any file is read."""
    run = store.start_transcript_run(owner.id, "p", TranscriptTrigger.AUTO)
    assert run.finished_at is None
    assert store.latest_transcript_run(owner.id, "p").finished_at is None


def test_finishing_a_run_records_its_counts(store, owner) -> None:
    run = store.start_transcript_run(owner.id, "p", TranscriptTrigger.MANUAL)
    done = store.finish_transcript_run(
        run.id,
        owner.id,
        files_seen=3,
        files_new=2,
        files_appended=1,
        files_rebuilt=0,
        lines_written=500,
        bytes_written=4096,
        anomalies=[{"path": "/tmp/x", "stored": 10, "on_disk": 4}],
        failures=[],
    )
    assert done.finished_at is not None
    assert done.files_new == 2
    assert done.anomalies[0]["on_disk"] == 4
    assert done.trigger is TranscriptTrigger.MANUAL


def test_event_session_ids_are_what_prove_a_directory_belongs_to_a_project(
    store, owner
) -> None:
    """Discovery intersects these with filenames on disk.

    This is the whole answer to the rename problem: a transcript filename IS
    a session id, and events already record which project a session belongs
    to, so ownership is proven rather than guessed from a directory slug.
    """
    record_event_for(store, owner, project="p", session_id="session-1")
    assert "session-1" in store.event_session_ids(owner.id, "p")
```

Define `record_event_for` at the top of the test file:

```python
def record_event_for(
    store: PostgresStore, owner: Principal, *, project: str, session_id: str
) -> None:
    """One recorded event for a session, which is all discovery needs.

    Discovery proves a directory belongs to a project by intersecting
    recorded session ids with transcript filenames, so the only field that
    matters here is `session_id`. Everything else is the shape `Event`
    requires.
    """
    store.put_event(
        Event(
            id=new_id(),
            owner_id=owner.id,
            project=project,
            harness="claude-code",
            session_id=session_id,
            kind=EventKind.TOOL_CALL,
            tool="Bash",
            payload={"command": "ls"},
            occurred_at=datetime(2026, 9, 15, tzinfo=UTC),
        )
    )
```

Imports it needs: `from datetime import UTC, datetime`, and from
`saddlebag.domain`: `Event`, `EventKind`, `Principal`, `new_id`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_store_transcripts.py -k "path or run or session_ids" -v`
Expected: FAIL - `AttributeError: ... has no attribute 'add_transcript_path'`.

- [ ] **Step 3: Implement the path methods**

```python
    def add_transcript_path(
        self, owner_id: UUID, project: str, path: str
    ) -> str | None:
        """Claim a directory, or name the project that already holds it.

        Returns None on success and the conflicting project's name on
        refusal, rather than a bare bool: told only "taken", a user has no
        way to find out by what, and the real failure would surface much
        later as a session filed under the wrong project.
        """
        with self._cur() as cur:
            # One statement, not a select-then-insert. Two concurrent claims
            # of the same path would both pass a "does it exist yet" check and
            # the loser would hit UniqueViolation from
            # transcript_paths_one_owner_idx - an unhandled crash in place of
            # the conflicting project name this method exists to return.
            #
            # `do update set project = transcript_paths.project` is a
            # deliberate no-op write. Its only job is to make the conflicting
            # row's project come back through `returning`; `do nothing`
            # returns no row at all, which would force the second round trip
            # this form is eliminating. Do not "tidy" the self-assignment
            # away - it is load-bearing.
            cur.execute(
                """
                insert into transcript_paths (owner_id, project, path)
                values (%s, %s, %s)
                on conflict (owner_id, path)
                  do update set project = transcript_paths.project
                returning project
                """,
                (owner_id, project, path),
            )
            # This module's cursor uses `dict_row`, so rows are read by column
            # name, never by position.
            holder = str(_one(cur)["project"])
            return None if holder == project else holder

    def remove_transcript_path(
        self, owner_id: UUID, project: str, path: str
    ) -> bool:
        with self._cur() as cur:
            cur.execute(
                "delete from transcript_paths"
                " where owner_id = %s and project = %s and path = %s",
                (owner_id, project, path),
            )
            return cur.rowcount == 1

    def transcript_paths(
        self, owner_id: UUID, project: str | None = None
    ) -> list[TranscriptPath]:
        """Claims for one project, or every claim when project is None.

        The sweep is what `bag record status` needs: it reports one advisory
        line per unhealthy claimed project, and unlike ingest's status it can
        answer for every project because the claim stores an absolute path
        and needs no recorded working directory to resolve.
        """
        where = "owner_id = %s" + ("" if project is None else " and project = %s")
        params: tuple[Any, ...] = (
            (owner_id,) if project is None else (owner_id, project)
        )
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select owner_id, project, path, added_at from transcript_paths
                 where {where}
                 order by project, path
                """),
                params,
            )
            return [_row_to_transcript_path(r) for r in cur.fetchall()]
```

Note the `where` fragment is built from module-local text and the owner and
project always travel as parameters - the `as_sql` contract.

- [ ] **Step 4: Implement the run methods**

Mirror `start_ingest_run` / `finish_ingest_run` / `latest_ingest_run` in the
same file exactly, substituting the transcript columns. Add a
`transcript_run_columns()` helper beside `ingest_run_columns()`. `anomalies`
and `failures` are written with `Jsonb(...)` and read back with
`_row_to_transcript_run`.

```python
Note on row access, which Task 3 had to correct in its own code: this
module's cursor is configured with `dict_row`, so every result row is read by
**column name**, never by position. Where a query returns a bare aggregate,
give it an alias (`select count(*) as count ...`) so there is a name to read.

    def event_session_ids(self, owner_id: UUID, project: str) -> list[str]:
        """Distinct session ids recorded for a project.

        Read-only and used only by `discover`, which intersects these with
        transcript filenames to prove which directory belongs to which
        project.
        """
        with self._cur() as cur:
            cur.execute(
                "select distinct session_id from events"
                " where owner_id = %s and project = %s",
                (owner_id, project),
            )
            return [str(r["session_id"]) for r in cur.fetchall()]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_store_transcripts.py -v`
Expected: PASS, 13 tests.

- [ ] **Step 6: Run the gate and commit**

```bash
make check
git add src/saddlebag/store.py src/saddlebag/backends/postgres/store.py tests/test_store_transcripts.py
git commit -m "Add store methods for transcript path claims and run rows

add_transcript_path returns the conflicting project rather than a bool:
told only 'taken', a user cannot find out by what, and the real failure
surfaces later as a session filed under the wrong project."
```

---

### Task 5: The service - discovery

**Files:**
- Create: `src/saddlebag/services/transcripts.py`
- Test: `tests/test_transcripts_discover.py`

**Interfaces:**
- Consumes: `store.event_session_ids`, `store.transcript_paths`.
- Produces: `Candidate` (dataclass: `path: str`, `matched: int`, `total: int`, `claimed_by: str | None`), `discover(store: Store, owner_id: UUID, project: str, root: Path) -> list[Candidate]`.

`root` is a parameter rather than a module constant so the tests never touch
the real `~/.claude`. The CLI passes `Path.home() / ".claude" / "projects"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_transcripts_discover.py`:

```python
"""Discovery proposes; it never writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from saddlebag.services import transcripts

pytestmark = pytest.mark.db


def _transcript(directory: Path, session_id: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{session_id}.jsonl").write_bytes(
        json.dumps({"type": "user"}).encode() + b"\n"
    )


def test_discover_proves_ownership_by_session_id_not_by_directory_name(
    store, owner, tmp_path: Path
) -> None:
    """The answer to the rename problem.

    A directory named for the OLD binary holds this project's sessions, and
    discovery finds it because transcript filenames are session ids and
    events already record which project each session belongs to.
    """
    old = tmp_path / "-Users-b-llmworkspace-oldname"
    _transcript(old, "sess-1")
    _transcript(old, "sess-2")
    _transcript(old, "never-recorded")
    record_event_for(store, owner, project="p", session_id="sess-1")
    record_event_for(store, owner, project="p", session_id="sess-2")

    found = transcripts.discover(store, owner.id, "p", tmp_path)

    assert len(found) == 1
    assert found[0].path == str(old)
    assert found[0].matched == 2
    assert found[0].total == 3


def test_discover_writes_nothing(store, owner, tmp_path: Path) -> None:
    """Widening scope is always a human act. This command only proposes."""
    old = tmp_path / "-dir"
    _transcript(old, "sess-1")
    record_event_for(store, owner, project="p", session_id="sess-1")
    transcripts.discover(store, owner.id, "p", tmp_path)
    assert store.transcript_paths(owner.id, "p") == []


def test_discover_marks_a_directory_already_claimed(
    store, owner, tmp_path: Path
) -> None:
    old = tmp_path / "-dir"
    _transcript(old, "sess-1")
    record_event_for(store, owner, project="p", session_id="sess-1")
    store.add_transcript_path(owner.id, "p", str(old))
    found = transcripts.discover(store, owner.id, "p", tmp_path)
    assert found[0].claimed_by == "p"


def test_discover_cannot_see_a_directory_with_no_recorded_sessions(
    store, owner, tmp_path: Path
) -> None:
    """The stated floor, not an oversight.

    A worktree directory holding 9 transcripts and zero recorded sessions is
    invisible here and claimable only by a human who knows it exists. That is
    exactly why discovery is a proposal rather than an algorithm.
    """
    _transcript(tmp_path / "-worktree", "unrecorded-1")
    assert transcripts.discover(store, owner.id, "p", tmp_path) == []
```

Define `record_event_for` at the top of the file, so the four tests share it:

```python
def record_event_for(
    store: PostgresStore, owner: Principal, *, project: str, session_id: str
) -> None:
    """One recorded event for a session, which is all discovery needs.

    Discovery proves a directory belongs to a project by intersecting
    recorded session ids with transcript filenames, so the only field that
    matters here is `session_id`. Everything else is the shape `Event`
    requires.
    """
    store.put_event(
        Event(
            id=new_id(),
            owner_id=owner.id,
            project=project,
            harness="claude-code",
            session_id=session_id,
            kind=EventKind.TOOL_CALL,
            tool="Bash",
            payload={"command": "ls"},
            occurred_at=datetime(2026, 9, 15, tzinfo=UTC),
        )
    )
```

Imports it needs: `from datetime import UTC, datetime`, and from
`saddlebag.domain`: `Event`, `EventKind`, `Principal`, `new_id`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_transcripts_discover.py -v`
Expected: FAIL - `ImportError: cannot import name 'transcripts'`.

- [ ] **Step 3: Write the service module and `discover`**

Create `src/saddlebag/services/transcripts.py`:

```python
"""Transcript capture: every policy decision for storing raw session traces.

Frontends parse and format; they never decide. The rules that live here -
what a claim means, when a file is re-read, what counts as an anomaly, how
much a spawned refresh may do - are ones every frontend gets for free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from saddlebag.store import Store

__all__ = [
    "Candidate",
    "discover",
]

#: The harness whose transcripts this reads. A constant rather than a
#: parameter because there is exactly one today and inventing the
#: generalisation before a second harness exists would be guessing at its
#: shape - the `harness` COLUMN is the part that costs nothing to have early.
HARNESS = "claude-code"


@dataclass
class Candidate:
    """A directory discovery believes belongs to a project, with its evidence."""

    path: str
    #: Transcripts in this directory whose session id has recorded events for
    #: this project. This is the proof; the name of the directory is not.
    matched: int
    #: Every transcript in the directory. Deliberately reported beside
    #: `matched`, because claiming imports all of them - including sessions
    #: from before recording existed, which can outnumber the matched ones
    #: several times over.
    total: int
    claimed_by: str | None


def discover(
    store: Store, owner_id: UUID, project: str, root: Path
) -> list[Candidate]:
    """Propose directories that hold this project's sessions. Writes nothing.

    Ownership is PROVEN, not guessed: a transcript's filename is a session
    id, and `events` already records which project each session belongs to.
    Matching on the directory slug instead would have missed 184MB of this
    project's own history, because the tool was renamed partway through.

    The floor, stated rather than papered over: a directory whose sessions
    were never recorded cannot be found here at all. That is why this
    proposes and `designate` decides.
    """
    recorded = set(store.event_session_ids(owner_id, project))
    if not recorded or not root.is_dir():
        return []

    claims = {p.path: p.project for p in store.transcript_paths(owner_id)}

    found: list[Candidate] = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        files = sorted(directory.glob("*.jsonl"))
        if not files:
            continue
        matched = sum(1 for f in files if f.stem in recorded)
        if matched == 0:
            continue
        found.append(
            Candidate(
                path=str(directory),
                matched=matched,
                total=len(files),
                claimed_by=claims.get(str(directory)),
            )
        )
    return found
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_transcripts_discover.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Run the gate and commit**

```bash
make check
git add src/saddlebag/services/transcripts.py tests/test_transcripts_discover.py
git commit -m "Add transcript discovery, proving ownership by session id

A transcript filename is a session id and events already record which
project a session belongs to, so a directory named for the old binary is
found on evidence rather than by name matching."
```

---

### Task 6: The service - designation

**Files:**
- Modify: `src/saddlebag/services/transcripts.py`
- Test: `tests/test_transcripts_designate.py`

**Interfaces:**
- Produces: `PathRefused` (Exception), `designate(store: Store, owner_id: UUID, project: str, path: Path) -> str` (returns the stored absolute path), `undesignate(store: Store, owner_id: UUID, project: str, path: Path) -> bool`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_transcripts_designate.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from saddlebag.services import transcripts

pytestmark = pytest.mark.db


def test_designate_stores_an_absolute_path(store, owner, tmp_path: Path) -> None:
    """Absolute, deliberately unlike reingest designate's repo-relative paths.

    These directories live outside any repository; there is no git root to
    resolve them against.
    """
    got = transcripts.designate(store, owner.id, "p", tmp_path)
    assert got == str(tmp_path.resolve())
    assert [c.path for c in store.transcript_paths(owner.id, "p")] == [got]


def test_designate_refuses_a_directory_that_does_not_exist(
    store, owner, tmp_path: Path
) -> None:
    """Loudly, because this is the one moment there is a human to tell."""
    with pytest.raises(transcripts.PathRefused, match="does not exist"):
        transcripts.designate(store, owner.id, "p", tmp_path / "nope")


def test_designate_refuses_a_file(store, owner, tmp_path: Path) -> None:
    target = tmp_path / "a.jsonl"
    target.write_text("{}")
    with pytest.raises(transcripts.PathRefused, match="not a directory"):
        transcripts.designate(store, owner.id, "p", target)


def test_designate_names_the_project_already_holding_the_directory(
    store, owner, tmp_path: Path
) -> None:
    transcripts.designate(store, owner.id, "alpha", tmp_path)
    with pytest.raises(transcripts.PathRefused, match="alpha"):
        transcripts.designate(store, owner.id, "beta", tmp_path)


def test_designating_the_same_directory_twice_is_not_an_error(
    store, owner, tmp_path: Path
) -> None:
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.designate(store, owner.id, "p", tmp_path)
    assert len(store.transcript_paths(owner.id, "p")) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_transcripts_designate.py -v`
Expected: FAIL - `AttributeError: module 'saddlebag.services.transcripts' has no attribute 'designate'`.

- [ ] **Step 3: Implement**

Add to `src/saddlebag/services/transcripts.py` (and to `__all__`):

```python
class PathRefused(Exception):
    """A directory cannot be claimed, and the message says why."""


def designate(store: Store, owner_id: UUID, project: str, path: Path) -> str:
    """Claim a transcript directory for a project. Fail-loud by design.

    This is the second opt-in and it deserves saying plainly: the per-project
    record gate governs recording going FORWARD, while claiming a directory
    imports all of it - including sessions that predate the pipeline
    entirely. Nothing auto-claims, which is why this refuses loudly rather
    than skipping: it is the one moment there is a human to tell.

    The path is stored absolute and resolved, deliberately unlike
    `reingest designate`, which stores repo-relative paths against a git
    root. These directories are outside any repository.
    """
    if not path.exists():
        raise PathRefused(f"{path} does not exist")
    if not path.is_dir():
        raise PathRefused(f"{path} is not a directory")

    absolute = str(path.resolve())
    holder = store.add_transcript_path(owner_id, project, absolute)
    if holder is not None:
        raise PathRefused(
            f"{absolute} is already claimed by project '{holder}' - "
            f"a directory belongs to one project, or the same session would "
            f"be filed under two"
        )
    return absolute


def undesignate(store: Store, owner_id: UUID, project: str, path: Path) -> bool:
    """Drop a claim. Transcripts already imported are NOT deleted.

    Same reasoning as the import's refusal to follow a shrunk file: this
    command stops future reading, and destroying stored sessions is a
    separate, explicit act. `bag transcripts prune` is the thing that would
    delete, and it does not exist yet.
    """
    return store.remove_transcript_path(owner_id, project, str(path.resolve()))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_transcripts_designate.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Run the gate and commit**

```bash
make check
git add src/saddlebag/services/transcripts.py tests/test_transcripts_designate.py
git commit -m "Add transcript directory designation, fail-loud

Claiming a directory is the second opt-in: the record gate covers
recording going forward, a claim covers backfill of everything already
there. It refuses loudly because that is the one moment there is a human
to tell."
```

---

### Task 7: The service - import

**Files:**
- Modify: `src/saddlebag/services/transcripts.py`
- Test: `tests/test_transcripts_import.py`

**Interfaces:**
- Produces: `Report` (dataclass with `files_seen`, `files_new`, `files_appended`, `files_rebuilt`, `lines_written`, `bytes_written`, `anomalies: list[dict[str, Any]]`, `failures: list[dict[str, Any]]`), `REFRESH_FILE_CAP: int = 25`, `run(store, owner_id, project, *, trigger: TranscriptTrigger, cap: int | None = None) -> Report`.

`cap` is `None` for the typed import (no bound) and `REFRESH_FILE_CAP` for the
spawned refresh. The cap is applied **before reading**, never after.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_transcripts_import.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from saddlebag.domain import TranscriptTrigger
from saddlebag.services import transcripts

pytestmark = pytest.mark.db


def _write(directory: Path, session_id: str, lines: list[dict]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{session_id}.jsonl"
    target.write_bytes(
        b"".join(json.dumps(line).encode() + b"\n" for line in lines)
    )
    return target


def test_import_stores_content_and_lines(store, owner, tmp_path: Path) -> None:
    _write(tmp_path, "s1", [{"type": "user"}, {"type": "assistant"}])
    transcripts.designate(store, owner.id, "p", tmp_path)

    report = transcripts.run(
        store, owner.id, "p", trigger=TranscriptTrigger.MANUAL
    )

    assert report.files_new == 1
    assert report.lines_written == 2
    stored = store.get_transcript(owner.id, transcripts.HARNESS, "s1")
    assert store.transcript_line_count(stored.id) == 2


def test_a_second_run_over_an_unchanged_file_reads_nothing(
    store, owner, tmp_path: Path
) -> None:
    """The common case at every session start: one stat, no read."""
    _write(tmp_path, "s1", [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    report = transcripts.run(
        store, owner.id, "p", trigger=TranscriptTrigger.MANUAL
    )

    assert report.files_seen == 1
    assert report.files_new == 0
    assert report.files_appended == 0
    assert report.lines_written == 0


def test_an_appended_file_adds_only_the_new_lines(
    store, owner, tmp_path: Path
) -> None:
    target = _write(tmp_path, "s1", [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    with target.open("ab") as fh:
        fh.write(json.dumps({"type": "assistant"}).encode() + b"\n")

    report = transcripts.run(
        store, owner.id, "p", trigger=TranscriptTrigger.MANUAL
    )

    assert report.files_appended == 1
    assert report.lines_written == 1
    stored = store.get_transcript(owner.id, transcripts.HARNESS, "s1")
    assert store.transcript_line_count(stored.id) == 2


def test_append_keeps_seq_aligned_with_the_file_across_a_bad_line(
    store, owner, tmp_path: Path
) -> None:
    """seq is the line's number in the FILE, not the count of rows stored.

    A line that fails to parse still occupies a line. If the append path
    derived its starting seq from the stored ROW count, every blank or
    unparseable line would shift every later seq by one - silently, and
    against the coordinate future labelling work keys on.
    """
    target = tmp_path / "s1.jsonl"
    target.write_bytes(b'{"type": "user"}\nthis is not json\n')
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    with target.open("ab") as fh:
        fh.write(b'{"type": "assistant"}\n')

    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    stored = store.get_transcript(owner.id, transcripts.HARNESS, "s1")
    assert store.transcript_line_count(stored.id) == 2
    # The appended line is the file's THIRD line, seq 2 - not seq 1, which is
    # what a row-count-derived start would have produced.
    with store._cur() as cur:  # noqa: SLF001 - asserting stored seqs directly
        cur.execute(
            "select seq from transcript_lines where transcript_id = %s order by seq",
            (stored.id,),
        )
        assert [r["seq"] for r in cur.fetchall()] == [0, 2]


def test_a_rewritten_file_rebuilds_its_lines(store, owner, tmp_path: Path) -> None:
    _write(tmp_path, "s1", [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    # Same session, different history, longer than before.
    _write(tmp_path, "s1", [{"type": "system"}, {"type": "assistant"}])

    report = transcripts.run(
        store, owner.id, "p", trigger=TranscriptTrigger.MANUAL
    )

    assert report.files_rebuilt == 1
    stored = store.get_transcript(owner.id, transcripts.HARNESS, "s1")
    assert store.transcript_line_count(stored.id) == 2


def test_a_shrunk_file_is_an_anomaly_and_is_not_followed(
    store, owner, tmp_path: Path
) -> None:
    """The stored copy is more complete. Two sessions are already gone from
    disk, and this is the rule that exists for exactly that."""
    _write(tmp_path, "s1", [{"type": "user"}, {"type": "assistant"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)
    before = store.get_transcript(owner.id, transcripts.HARNESS, "s1")

    _write(tmp_path, "s1", [{"type": "user"}])

    report = transcripts.run(
        store, owner.id, "p", trigger=TranscriptTrigger.MANUAL
    )

    assert len(report.anomalies) == 1
    assert report.anomalies[0]["stored"] == before.bytes
    after = store.get_transcript(owner.id, transcripts.HARNESS, "s1")
    assert after.bytes == before.bytes


def test_an_unparseable_line_is_named_not_dropped(
    store, owner, tmp_path: Path
) -> None:
    tmp_path.joinpath("s1.jsonl").write_bytes(
        b'{"type": "user"}\nthis is not json\n'
    )
    transcripts.designate(store, owner.id, "p", tmp_path)

    report = transcripts.run(
        store, owner.id, "p", trigger=TranscriptTrigger.MANUAL
    )

    assert report.lines_written == 1
    assert len(report.failures) == 1
    assert "not json" in report.failures[0]["reason"] or "line 1" in (
        report.failures[0]["reason"]
    )


def test_the_cap_bounds_a_refresh_before_it_reads(
    store, owner, tmp_path: Path
) -> None:
    """A session-start hook must never read 179MB.

    Enforced before reading, not after, which is the whole point.
    """
    for i in range(5):
        _write(tmp_path, f"s{i}", [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)

    report = transcripts.run(
        store, owner.id, "p", trigger=TranscriptTrigger.AUTO, cap=2
    )

    assert report.files_new == 2


def test_an_unclaimed_project_does_nothing(store, owner, tmp_path: Path) -> None:
    """The common case, and the entire opt-in."""
    _write(tmp_path, "s1", [{"type": "user"}])
    report = transcripts.run(
        store, owner.id, "p", trigger=TranscriptTrigger.AUTO
    )
    assert report.files_seen == 0


def test_a_missing_claimed_directory_is_a_failure_not_a_crash(
    store, owner, tmp_path: Path
) -> None:
    transcripts.designate(store, owner.id, "p", tmp_path)
    tmp_path.rmdir()
    report = transcripts.run(
        store, owner.id, "p", trigger=TranscriptTrigger.MANUAL
    )
    assert len(report.failures) == 1


def test_the_run_is_recorded_with_its_trigger(store, owner, tmp_path: Path) -> None:
    _write(tmp_path, "s1", [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.AUTO)
    run = store.latest_transcript_run(owner.id, "p")
    assert run.trigger is TranscriptTrigger.AUTO
    assert run.finished_at is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_transcripts_import.py -v`
Expected: FAIL - `module 'saddlebag.services.transcripts' has no attribute 'run'`.

- [ ] **Step 3: Implement**

Add to `src/saddlebag/services/transcripts.py`:

```python
#: How many files a spawned refresh may read in one run.
#:
#: The first import of a claimed directory is 179MB across 146 files, which
#: must never happen inside a session-start hook. The cap is applied BEFORE
#: reading rather than after, so a bounded run is bounded in I/O and not
#: merely in what it reports. The typed `bag transcripts import` passes None
#: and does everything, because a person asked for that.
REFRESH_FILE_CAP = 25


@dataclass
class Report:
    """What one import did. Mutated in place so a partial run is recorded."""

    files_seen: int = 0
    files_new: int = 0
    files_appended: int = 0
    files_rebuilt: int = 0
    lines_written: int = 0
    bytes_written: int = 0
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)


def run(
    store: Store,
    owner_id: UUID,
    project: str,
    *,
    trigger: TranscriptTrigger,
    cap: int | None = None,
) -> Report:
    """Import every claimed directory for a project, recording the run.

    Split into a recording wrapper and `_run_body` for the reason
    `memory.sync` is: the wrapper owns the row, and the body is handed the
    `Report` it mutates, so a run that raises partway is still recorded with
    what it had done. A Python exception is recorded as a failure with path
    `*` and re-raised.
    """
    report = Report()
    started = store.start_transcript_run(owner_id, project, trigger)
    try:
        _run_body(store, owner_id, project, cap, report)
    except Exception as exc:
        report.failures.append({"path": "*", "reason": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        store.finish_transcript_run(
            started.id,
            owner_id,
            files_seen=report.files_seen,
            files_new=report.files_new,
            files_appended=report.files_appended,
            files_rebuilt=report.files_rebuilt,
            lines_written=report.lines_written,
            bytes_written=report.bytes_written,
            anomalies=report.anomalies,
            failures=report.failures,
        )
    return report


def _run_body(
    store: Store,
    owner_id: UUID,
    project: str,
    cap: int | None,
    report: Report,
) -> None:
    budget = cap
    for claim in store.transcript_paths(owner_id, project):
        directory = Path(claim.path)
        if not directory.is_dir():
            # A claimed directory that has gone is reported on every run,
            # which is how a moved or deleted directory stops being a silent
            # per-run no-op. Same treatment ingest gives a missing path.
            report.failures.append(
                {"path": claim.path, "reason": "claimed directory does not exist"}
            )
            continue
        for path in sorted(directory.glob("*.jsonl")):
            if budget is not None and budget <= 0:
                return
            report.files_seen += 1
            did_work = _import_one(store, owner_id, project, path, report)
            if did_work and budget is not None:
                budget -= 1


def _import_one(
    store: Store,
    owner_id: UUID,
    project: str,
    path: Path,
    report: Report,
) -> bool:
    """Import or update one transcript. True when it did any reading.

    The budget is spent only on files that were actually read, so a refresh
    over a directory of unchanged transcripts costs one stat each and skips
    nothing it could have done.
    """
    session_id = path.stem
    existing = store.get_transcript(owner_id, HARNESS, session_id)

    try:
        disk_size = path.stat().st_size
    except OSError as exc:
        report.failures.append({"path": str(path), "reason": str(exc)})
        return False

    if existing is None:
        return _store_whole(store, owner_id, project, path, report, new=True)

    plan = _plan_for(existing, path, disk_size, report)
    if plan is ReadPlan.SKIP or plan is ReadPlan.SHRUNK:
        return False
    if plan is ReadPlan.REBUILD:
        return _store_whole(store, owner_id, project, path, report, new=False)
    return _append(store, owner_id, existing, path, report)


def _plan_for(
    existing: Transcript, path: Path, disk_size: int, report: Report
) -> ReadPlan:
    """Classify, reading the prefix only when the size actually grew."""
    prefix_sha: str | None = None
    if disk_size > existing.bytes:
        try:
            with path.open("rb") as fh:
                prefix_sha = sha256_hex(fh.read(existing.bytes))
        except OSError as exc:
            report.failures.append({"path": str(path), "reason": str(exc)})
            return ReadPlan.SKIP

    plan = classify(existing.bytes, existing.sha256, disk_size, prefix_sha)
    if plan is ReadPlan.SHRUNK:
        # Recorded and NOT followed. The stored copy is more complete than
        # what is on disk, and the purpose of the source row is that a
        # rotating file does not destroy the session.
        report.anomalies.append(
            {"path": str(path), "stored": existing.bytes, "on_disk": disk_size}
        )
    return plan


def _store_whole(
    store: Store,
    owner_id: UUID,
    project: str,
    path: Path,
    report: Report,
    *,
    new: bool,
) -> bool:
    try:
        content = path.read_bytes()
    except OSError as exc:
        report.failures.append({"path": str(path), "reason": str(exc)})
        return False

    stored = store.put_transcript(
        owner_id, project, HARNESS, path.stem, str(path), content,
        sha256_hex(content),
    )
    lines, failures = parse(content)
    store.replace_transcript_lines(stored.id, lines)

    report.lines_written += len(lines)
    report.bytes_written += len(content)
    report.failures.extend(
        {"path": str(path), "reason": f.reason} for f in failures
    )
    if new:
        report.files_new += 1
    else:
        report.files_rebuilt += 1
    return True


def _append(
    store: Store,
    owner_id: UUID,
    existing: Transcript,
    path: Path,
    report: Report,
) -> bool:
    try:
        with path.open("rb") as fh:
            fh.seek(existing.bytes)
            tail = fh.read()
    except OSError as exc:
        report.failures.append({"path": str(path), "reason": str(exc)})
        return False

    # Fetched once and used twice: for the whole-file hash that the NEXT
    # append check compares against, and for the tail's first line number.
    stored = store.transcript_content(existing.id, owner_id) or b""

    # seq is the line's number within the FILE, and `parse` numbers lines by
    # their index in `split(b"\n")` - which counts blank lines and lines that
    # failed to parse, because each still occupies a line in the file. The
    # stored ROW count counts neither, so using it here would drift seq by one
    # for every blank or unparseable line ever seen, silently - and
    # (transcript_id, seq) is the coordinate future labelling work keys on.
    # Counting newlines in the stored bytes is exact: the tail begins
    # immediately after the last stored byte, so its first line is line number
    # `stored.count(b"\n")`.
    start_seq = stored.count(b"\n")

    if not store.append_transcript(
        existing.id, owner_id, tail, sha256_hex(stored + tail)
    ):
        report.failures.append(
            {"path": str(path), "reason": "append refused - not this owner"}
        )
        return False

    lines, failures = parse(tail, start_seq=start_seq)
    store.add_transcript_lines(existing.id, lines)

    report.files_appended += 1
    report.lines_written += len(lines)
    report.bytes_written += len(tail)
    report.failures.extend(
        {"path": str(path), "reason": f.reason} for f in failures
    )
    return True
```

Add the imports this needs at the top of the module: `Any` from `typing`,
`TranscriptTrigger` and `Transcript` from `saddlebag.domain`, and `ReadPlan`,
`classify`, `parse`, `sha256_hex` from `saddlebag.transcript_file`. Add
`Report`, `REFRESH_FILE_CAP` and `run` to `__all__`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_transcripts_import.py -v`
Expected: PASS, 10 tests.

- [ ] **Step 5: Run the gate and commit**

```bash
make check
git add src/saddlebag/services/transcripts.py tests/test_transcripts_import.py
git commit -m "Add the transcript import, bounded and append-aware

An unchanged file costs one stat. A grown file is proven append-only by
its prefix hash before any line is added. A shrunk file is recorded as an
anomaly and NOT followed - the stored copy is more complete, and two
sessions are already gone from disk."
```

---

### Task 8: CLI commands

**Files:**
- Modify: `src/saddlebag/cli.py`
- Test: `tests/test_transcripts_cli.py`

**Interfaces:**
- Produces: `bag transcripts discover`, `bag transcripts designate <dir>`, `bag transcripts import`, `bag transcripts refresh`, `bag transcripts status [--json]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_transcripts_cli.py`. These commit through `open_session()`,
so they need the `live_dsn` fixture rather than `conn` - copy the environment
setup verbatim from the top of `tests/test_memory_cli.py` (`grep -n "live_dsn"
tests/test_memory_cli.py` shows how it points `BAG_DSN` at the scratch
database and stubs the spawn helpers).

```python
"""The transcripts sub-app. Commits, so it uses live_dsn."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from saddlebag.cli import app

pytestmark = pytest.mark.db

runner = CliRunner()


def test_discover_prints_its_evidence_and_writes_nothing(
    cli_env, tmp_path: Path
) -> None:
    """The counts are the point: claiming imports `total`, not `matched`."""
    result = runner.invoke(app, ["transcripts", "discover"])
    assert result.exit_code == 0
    assert "2 of 3" in result.stdout


def test_designate_echoes_the_backfill_command_and_its_cost(
    cli_env, tmp_path: Path
) -> None:
    """Claiming must not silently commit someone to a large read.

    The command records a claim and imports nothing, so the output has to
    name what comes next and roughly what it will cost.
    """
    result = runner.invoke(app, ["transcripts", "designate", str(tmp_path)])
    assert result.exit_code == 0
    assert "bag transcripts import" in result.stdout


def test_designate_exits_non_zero_when_refused(cli_env) -> None:
    result = runner.invoke(app, ["transcripts", "designate", "/does/not/exist"])
    assert result.exit_code == 1
    assert "does not exist" in result.stdout


def test_refresh_exits_zero_and_prints_nothing_on_stdout(cli_env) -> None:
    """Fail-soft, like every spawned half in this repo."""
    result = runner.invoke(app, ["transcripts", "refresh"])
    assert result.exit_code == 0
    assert result.stdout == ""


def test_refresh_exits_zero_with_an_unreachable_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_session` raises typer.Exit(1) here, which must not escape."""
    monkeypatch.setenv("BAG_DSN", "postgresql://nobody@127.0.0.1:1/none")
    result = runner.invoke(app, ["transcripts", "refresh"])
    assert result.exit_code == 0


def test_refresh_survives_a_library_calling_sys_exit(
    monkeypatch: pytest.MonkeyPatch, cli_env
) -> None:
    """What the `except BaseException` is actually for.

    The unreachable-database case above passes under `except Exception` too,
    because typer.Exit subclasses RuntimeError - so it proves nothing about
    this guard. A genuine SystemExit does.
    """
    from saddlebag.services import transcripts

    def boom(*args: object, **kwargs: object) -> None:
        raise SystemExit(3)

    monkeypatch.setattr(transcripts, "run", boom)
    result = runner.invoke(app, ["transcripts", "refresh"])
    assert result.exit_code == 0


def test_status_json_has_the_same_keys_when_nothing_is_claimed(cli_env) -> None:
    """One object, not a list, and never a shorter document.

    A consumer checks a field for null rather than branching on which keys
    arrived - the same rule `bag memory status --json` follows, and
    deliberately not `bag reingest status --json`, which sweeps every project
    and so returns an array.
    """
    result = runner.invoke(app, ["transcripts", "status", "--json"])
    assert result.exit_code == 0
    got = json.loads(result.stdout)
    assert set(got) == {"project", "paths", "run", "backlog", "irrecoverable"}
    assert got["run"] is None
```

`cli_env` is a fixture you write at the top of this file: it points `BAG_DSN`
at `live_dsn`, creates the owner, stubs all four `hookio.spawn_*` helpers, and
seeds a transcript directory with three files, two of whose session ids have
recorded events. Copying `tests/test_memory_cli.py`'s equivalent is faster
than inventing one.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_transcripts_cli.py -v`
Expected: FAIL - `No such command 'transcripts'`.

- [ ] **Step 3: Implement the sub-app and commands**

In `src/saddlebag/cli.py`, beside the other sub-apps:

```python
transcripts_app = typer.Typer(help="Raw session transcripts, stored in saddlebag.")
app.add_typer(transcripts_app, name="transcripts")
```

Then the five commands. `refresh` copies `memory_refresh` exactly, including
the `except BaseException` and the comment explaining that it is NOT there for
`typer.Exit`:

```python
@transcripts_app.command("refresh")
def transcripts_refresh():
    """Import this project's transcripts. Spawned, not typed.

    The silent half of `bag transcripts import`, and the exact analogue of
    `bag memory refresh` and `bag reingest run`: started detached by a
    session start, so it exits 0 on every path, prints nothing to stdout, and
    explains itself only to stderr behind BAG_HOOK_DEBUG. A project with no
    claimed directory does nothing, which is the common case.

    Bounded at REFRESH_FILE_CAP files, enforced before reading: the first
    import of a claimed directory is 179MB, and a session start must never
    pay for it. `bag transcripts import` is the unbounded half, and a person
    asked for that one.
    """
    from saddlebag import hookio

    env = dict(os.environ)
    try:
        _transcripts_refresh_once(env)
    except BaseException as exc:
        # BaseException, not Exception, and the reason is narrower than it
        # looks. `typer.Exit` is a RuntimeError, so `except Exception`
        # already swallows the `typer.Exit(1)` `_session` raises for an
        # unreachable database - measured, not assumed. What BaseException
        # adds is a real SystemExit from any library that calls sys.exit(),
        # and a KeyboardInterrupt.
        hookio.debug(env, f"{type(exc).__name__}: {exc}")
    raise typer.Exit(0)
```

`designate` must echo the backfill command and the directory's file count, so
that claiming never silently commits someone to a large read. `status` renders
the latest run in **four distinct spellings** - never, clean, with failures or
anomalies, did not finish - and names the trigger in every spelling that has a
run.

- [ ] **Step 4: Run the tests, the gate, and commit**

```bash
uv run pytest tests/test_transcripts_cli.py -v
make check
git add src/saddlebag/cli.py tests/test_transcripts_cli.py
git commit -m "Add the bag transcripts commands

refresh is the spawned, silent, bounded half; import is the typed,
loud, unbounded one. status --json emits one object with the same keys in
every state, like bag memory status and unlike bag reingest status."
```

---

### Task 9: The spawn point

**Files:**
- Modify: `src/saddlebag/hookio.py`, `src/saddlebag/agents/claude_code/hook.py`, `src/saddlebag/cli.py` (the `hook context` command)
- Test: `tests/test_hook_spawn_transcripts.py` (create, following `test_hook_spawn_process.py`), `tests/test_hook_context_cli.py` (extend)

**Interfaces:**
- Produces: `hookio.spawn_transcripts(env: Mapping[str, str]) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
def test_spawn_transcripts_refuses_to_recurse(monkeypatch) -> None:
    """The fourth CHILD_ENV_VAR check.

    The extractor spawns `claude -p`, whose own hooks would otherwise spawn
    a transcript import, which would read the transcript that child is
    writing, without bound.
    """
    assert hookio.spawn_transcripts({CHILD_ENV_VAR: "1"}) is False


def test_session_start_spawns_a_transcript_import(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(hookio, "spawn_transcripts", lambda env: calls.append("t"))
    ...
    assert calls == ["t"]


def test_hook_context_spawns_transcripts_even_when_it_returns_no_block(...) -> None:
    """The call sits in a `finally`, like spawn_process's.

    The backlog is global: whether THIS payload produced a block says
    nothing about whether there are transcripts waiting.
    """
```

**Every test whose code path reaches a spawn helper must stub it.** A real
detached `bag` at `live_dsn` outlives the test and deadlocks conftest's
truncate. This rule already exists for `spawn_process`, `spawn_ingest` and
`spawn_memory`; `spawn_transcripts` is the fourth.

These are the files that already stub `spawn_memory`, and every one of them
needs `spawn_transcripts` stubbed alongside it:

- `tests/test_handoff_pointer.py`
- `tests/test_hook.py`
- `tests/test_hook_context_cli.py`
- `tests/test_hook_spawn_ingest.py`
- `tests/test_hook_spawn_memory.py`

Verify none were missed before committing:

```bash
grep -rln --include="*.py" "spawn_memory" tests/ | while read f; do
  grep -q "spawn_transcripts" "$f" || echo "MISSING stub: $f"
done
```

That loop printing nothing is the check. A missed stub does not fail loudly -
it launches a real detached `bag` against the test database, which outlives
the test and deadlocks conftest's truncate on some later, unrelated run.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_hook_spawn_transcripts.py -v`
Expected: FAIL - `module 'saddlebag.hookio' has no attribute 'spawn_transcripts'`.

- [ ] **Step 3: Implement**

```python
def spawn_transcripts(env: Mapping[str, str]) -> bool:
    """Start a detached `bag transcripts refresh` and return immediately.

    The fourth spawn, from the same two places as the other three - Claude
    Code's SessionStart and `bag hook context` - because that pair is the one
    trigger all three harnesses share. Fixed once, rather than bolted onto
    each new install path.

    A project with no claimed transcript directory does nothing, so this is a
    no-op for everyone who has not opted in. Separate from `spawn_memory` for
    the same reason that one is separate from `spawn_ingest`: the jobs share
    their trigger and nothing else, and neither should be able to delay the
    other.

    `bag transcripts refresh`, not `import`: the refresh is bounded before it
    reads, and a session start must never pay for a 179MB backfill.
    """
    return _spawn(["bag", "transcripts", "refresh"], env)
```

Then call it from `hook.session_start` beside the existing three spawns, and
from `bag hook context` **inside the same `finally`** that already calls
`spawn_process`, `spawn_ingest` and `spawn_memory`.

- [ ] **Step 4: Run the full suite, the gate, and commit**

```bash
uv run pytest -q
make check
git add src/saddlebag/hookio.py src/saddlebag/agents/claude_code/hook.py src/saddlebag/cli.py tests/
git commit -m "Spawn a transcript refresh from the shared session-start trigger

The fourth spawn, from the same two places as the other three, and the
fourth CHILD_ENV_VAR check - without it the extractor's own claude -p
child writes a transcript the next refresh imports, without bound."
```

---

### Task 10: Status, advisories, and the contract tests

**Files:**
- Modify: `src/saddlebag/services/transcripts.py` (add `status`, `status_to_dict`, `advisories`), `src/saddlebag/cli.py` (`record status`)
- Create: `tests/fixtures/transcript-sample.jsonl`, `tests/test_transcript_contract.py`
- Modify: `pyproject.toml` (register the `transcript` marker)
- Test: `tests/test_transcripts_status.py`

**Interfaces:**
- Produces: `TranscriptStatus` dataclass, `status(store, owner_id, project) -> TranscriptStatus`, `status_to_dict(got) -> dict[str, Any]`, `advisories(store, owner_id) -> list[str]`.

- [ ] **Step 1: Register the marker**

In `pyproject.toml`, add to `markers`:

```toml
    "transcript: requires a real Claude Code transcript in ~/.claude/projects; checks our parser still handles every line type",
```

- [ ] **Step 2: Write the always-runs contract test**

Create `tests/fixtures/transcript-sample.jsonl` **by hand**, synthetic, never
copied from a real session - a real transcript contains real code, real paths
and whatever the session touched. It must contain one line of each type seen
in the wild (`assistant`, `user`, `attachment`, `system`, `last-prompt`,
`mode`, `permission-mode`, `atis-latch`, `ai-title`, `file-history-snapshot`,
`cost-state`), one malformed line, and one line with an invalid UTF-8 byte.

Create `tests/test_transcript_contract.py`:

```python
"""Two tests, deliberately not one.

The first always runs, on CI and everywhere else, and catches a parser that
breaks on a shape we already know about. The second may skip, and only
guards the freshness of our understanding of a format Claude Code changes
without telling us. Collapsing them would produce a guard that skips on CI -
the failure mode the db markers already taught this project to distrust.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from saddlebag.transcript_file import parse

FIXTURE = Path(__file__).parent / "fixtures" / "transcript-sample.jsonl"

#: Every line type the fixture covers, which is every type observed in a real
#: transcript as of 2026-09-15. A type NOT in here is not an error - the
#: parser nulls the column and stores the line - but it is worth knowing.
KNOWN_TYPES = {
    "assistant",
    "user",
    "attachment",
    "system",
    "last-prompt",
    "mode",
    "permission-mode",
    "atis-latch",
    "ai-title",
    "file-history-snapshot",
    "file-history-delta",
    "cost-state",
    "queue-operation",
}


def test_the_parser_handles_every_line_type_we_have_seen() -> None:
    content = FIXTURE.read_bytes()
    lines, failures = parse(content)
    real = sum(1 for line in content.split(b"\n") if line.strip())
    assert len(lines) + len(failures) == real
    assert {line.type for line in lines if line.type} <= KNOWN_TYPES


@pytest.mark.transcript
def test_a_real_transcript_still_parses(tmp_path: Path) -> None:
    """May skip. Claude Code changes its format without telling us.

    Expect this to fire eventually, the way opencode's freshness test did
    mid-branch. When it does, the fix is to add the new type to KNOWN_TYPES
    and to the fixture - not to make the parser stricter.
    """
    root = Path.home() / ".claude" / "projects"
    found = sorted(root.glob("*/*.jsonl")) if root.is_dir() else []
    if not found:
        pytest.skip(f"no transcript under {root}")
    lines, failures = parse(found[-1].read_bytes())
    assert lines, "a real transcript parsed to no lines at all"
    unknown = {line.type for line in lines if line.type} - KNOWN_TYPES
    assert not unknown, f"new line types: {sorted(unknown)}"
```

- [ ] **Step 3: Run both, confirm the first passes and the second runs or skips**

Run: `uv run pytest tests/test_transcript_contract.py -v -rs`
Expected: the first PASSES; the second passes or skips with its reason printed.

- [ ] **Step 4: Write the failing status tests**

Create `tests/test_transcripts_status.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from saddlebag.domain import TranscriptTrigger
from saddlebag.services import transcripts

pytestmark = pytest.mark.db


def test_status_of_an_unclaimed_project_is_a_full_document(
    store, owner, tmp_path: Path
) -> None:
    """Same keys in every state, so a consumer checks a field for null
    rather than branching on which keys arrived."""
    got = transcripts.status_to_dict(
        transcripts.status(store, owner.id, "p", tmp_path)
    )
    assert set(got) == {"project", "paths", "run", "backlog", "irrecoverable"}
    assert got["run"] is None
    assert got["paths"] == []


def test_status_reports_a_missing_claimed_directory_as_missing(
    store, owner, tmp_path: Path
) -> None:
    """Never as clean. A directory that has gone is a definite statement."""
    claimed = tmp_path / "gone"
    claimed.mkdir()
    transcripts.designate(store, owner.id, "p", claimed)
    claimed.rmdir()
    got = transcripts.status(store, owner.id, "p", tmp_path)
    assert got.paths[0].present is False


def test_status_counts_the_backlog(store, owner, tmp_path: Path) -> None:
    for i in range(3):
        (tmp_path / f"s{i}.jsonl").write_bytes(b'{"type": "user"}\n')
    transcripts.designate(store, owner.id, "p", tmp_path)
    assert transcripts.status(store, owner.id, "p", tmp_path).backlog == 3


# `record_event_for` is the same helper Task 5's test file defines - copy it
# here rather than importing across test files.


def test_status_counts_sessions_whose_transcript_is_gone(
    store, owner, tmp_path: Path
) -> None:
    """This number only grows, and it is the argument for importing sooner
    stated as a measurement rather than as urgency."""
    record_event_for(store, owner, project="p", session_id="vanished")
    got = transcripts.status(store, owner.id, "p", tmp_path)
    assert got.irrecoverable == 1


def test_advisories_name_a_run_that_did_not_finish(
    store, owner, tmp_path: Path
) -> None:
    transcripts.designate(store, owner.id, "p", tmp_path)
    store.start_transcript_run(owner.id, "p", TranscriptTrigger.AUTO)
    lines = transcripts.advisories(store, owner.id)
    assert any("did not finish" in line for line in lines)


def test_advisories_say_nothing_about_backlog_alone(
    store, owner, tmp_path: Path
) -> None:
    """A refresh is bounded by design, so a nonzero backlog is the normal
    state between runs. A line that fires every time is ignored."""
    (tmp_path / "s1.jsonl").write_bytes(b'{"type": "user"}\n')
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL, cap=0)
    assert transcripts.advisories(store, owner.id) == []


def test_advisories_sweep_every_claimed_project(store, owner, tmp_path: Path) -> None:
    """Unlike ingest's status, this can answer for every project: a claim
    stores an absolute path and needs no recorded working directory."""
    for name in ("alpha", "beta"):
        directory = tmp_path / name
        directory.mkdir()
        transcripts.designate(store, owner.id, name, directory)
        directory.rmdir()
    lines = transcripts.advisories(store, owner.id)
    assert len(lines) == 2
```

- [ ] **Step 5: Run to verify it fails**

Run: `uv run pytest tests/test_transcripts_status.py -v`
Expected: FAIL - `module 'saddlebag.services.transcripts' has no attribute 'status'`.

- [ ] **Step 6: Implement status and advisories**

Add to `src/saddlebag/services/transcripts.py` (and to `__all__`):

```python
@dataclass
class PathStatus:
    """A claimed directory, and whether it is still there."""

    path: str
    present: bool
    on_disk: int


@dataclass
class TranscriptStatus:
    """What `bag transcripts status` answers for one project.

    `run` describes what last HAPPENED; `paths`, `backlog` and
    `irrecoverable` describe the state NOW. A reader must not have to infer
    one from the other, which is why both are here rather than only the run.
    """

    project: str
    paths: list[PathStatus]
    run: TranscriptRun | None
    backlog: int
    irrecoverable: int


def status(
    store: Store, owner_id: UUID, project: str, root: Path
) -> TranscriptStatus:
    """The current state of transcript capture for one project."""
    claims = store.transcript_paths(owner_id, project)
    paths: list[PathStatus] = []
    on_disk_ids: set[str] = set()
    for claim in claims:
        directory = Path(claim.path)
        present = directory.is_dir()
        files = sorted(directory.glob("*.jsonl")) if present else []
        on_disk_ids.update(f.stem for f in files)
        paths.append(
            PathStatus(path=claim.path, present=present, on_disk=len(files))
        )

    stored = {t.session_id for t in store.stored_transcripts(owner_id, project)}
    recorded = set(store.event_session_ids(owner_id, project))

    # "Anywhere" means anywhere under the transcript root, not only under a
    # claimed directory: a session whose file sits in an unclaimed directory
    # is recoverable by claiming it, and calling that irrecoverable would
    # overstate the loss.
    everywhere = (
        {f.stem for f in root.glob("*/*.jsonl")} if root.is_dir() else set()
    )

    return TranscriptStatus(
        project=project,
        paths=paths,
        run=store.latest_transcript_run(owner_id, project),
        backlog=len(on_disk_ids - stored),
        irrecoverable=len(recorded - stored - everywhere),
    )


def status_to_dict(got: TranscriptStatus) -> dict[str, Any]:
    """One object, not a list, and never a shorter document.

    The keys are the same in every state - an unclaimed project is a null
    `run` and an empty `paths`, not fewer keys - so a consumer checks a field
    for null rather than branching on which keys arrived. Timestamps are a
    raw `isoformat()`: the offset travels in the string.
    """
    return {
        "project": got.project,
        "paths": [
            {"path": p.path, "present": p.present, "on_disk": p.on_disk}
            for p in got.paths
        ],
        "run": None if got.run is None else _run_to_dict(got.run),
        "backlog": got.backlog,
        "irrecoverable": got.irrecoverable,
    }


def advisories(store: Store, owner_id: UUID) -> list[str]:
    """One line per unhealthy claimed project, for `bag record status`.

    Sweeps EVERY claimed project, which it can because a claim stores an
    absolute path and needs no recorded working directory to resolve - unlike
    `ingest.status`, which can only check the project the current directory
    resolves to.

    Backlog is deliberately not here. A refresh is bounded before it reads,
    so a nonzero backlog is the normal state between runs, and an advisory
    that fires on every run is one people learn to ignore.
    """
    lines: list[str] = []
    by_project: dict[str, list[str]] = {}
    for claim in store.transcript_paths(owner_id):
        by_project.setdefault(claim.project, []).append(claim.path)

    for project, paths in sorted(by_project.items()):
        missing = [p for p in paths if not Path(p).is_dir()]
        if missing:
            lines.append(
                f"! transcripts '{project}': claimed directory missing "
                f"({', '.join(missing)}) - run `bag transcripts status`"
            )
            continue

        run = store.latest_transcript_run(owner_id, project)
        if run is None:
            lines.append(
                f"! transcripts '{project}': claimed but never imported - "
                f"run `bag transcripts import`"
            )
        elif run.finished_at is None:
            lines.append(
                f"! transcripts '{project}': the last import ({run.trigger}) "
                f"did not finish - run `bag transcripts status`"
            )
        elif run.failures:
            lines.append(
                f"! transcripts '{project}': {len(run.failures)} failure(s) in "
                f"the last import ({run.trigger}) - run `bag transcripts status`"
            )
        elif run.anomalies:
            lines.append(
                f"! transcripts '{project}': {len(run.anomalies)} transcript(s) "
                f"shrank on disk and were not followed - "
                f"run `bag transcripts status`"
            )
    return lines
```

Write `_run_to_dict` beside it, mirroring `ingest.status_to_dict`'s run
rendering field for field.

- [ ] **Step 7: Run the status tests to verify they pass**

Run: `uv run pytest tests/test_transcripts_status.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 8: Wire the advisories into `bag record status`**

In `src/saddlebag/cli.py`'s `record status` command, call
`transcripts.advisories(session.store, session.owner.id)` beside the existing
ingest, memory and budget advisory calls, and echo each line. Add one test
asserting a claimed-but-never-imported project produces a line there.

- [ ] **Step 9: Run the gate and commit**

```bash
make check
git add pyproject.toml src/saddlebag/services/transcripts.py src/saddlebag/cli.py tests/
git commit -m "Add transcript status, advisories, and the two contract tests

The always-runs test uses a synthetic fixture; the marked one re-reads a
real transcript and may skip. Split for the reason the opencode hook
contract is split - one test would skip on CI and guard nothing."
```

---

### Task 11: Documentation

**Files:**
- Modify: `CLAUDE.md` (new section after "Ingested documents")

- [ ] **Step 1: Write the section**

Add a `### Session transcripts` section covering, in the density of the
surrounding prose: what is stored and why the events pipeline was not enough;
that ownership of a directory is proven by session id and never by name;
that claiming a directory is the second opt-in and imports everything in it;
that `transcript_lines` is derived and nothing may store anything only there;
that a shrunk file is an anomaly and is not followed; that the refresh is
bounded before it reads while `import` is not; and that redaction is deferred
to a future export, deliberately, with the reasoning.

- [ ] **Step 2: Verify and commit**

```bash
make check
git add CLAUDE.md
git commit -m "Document transcript capture in CLAUDE.md"
```

---

## Verification

After the final task:

```bash
docker compose up -d
make check
```

Expected: exit 0, **and a skip count you have actually read**. Most of this
feature is `db`-marked; a green run with everything skipped proves nothing.
Confirm with:

```bash
uv run pytest -q -m db 2>&1 | tail -3
```

Then the real proof, against real data:

```bash
bag transcripts discover
bag transcripts designate ~/.claude/projects/-Users-brandon-llmworkspace-remem
bag transcripts import
bag transcripts status
```

Expected: `discover` proposes the `-remem` directory with 26 of 35 matched
sessions and 112 transcripts total; `import` stores them; `status` reports a
clean manual run, zero backlog, and an irrecoverable count of 2.
