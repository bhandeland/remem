# Subagent Transcripts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store Claude Code's subagent transcripts
(`<dir>/<session>/subagents/agent-<id>.jsonl`) byte-exact beside the
session transcripts the import already captures, and make `status`,
`discover` and `designate` say so.

**Architecture:** One nullable `agent_id` column on `transcripts` (NULL
for a session's own transcript), with the unique key widened to
`(owner_id, harness, session_id, agent_id) nulls not distinct`. A single
function, `transcript_files()`, owns the on-disk layout and hands every
caller `(path, session_id, agent_id)`; nothing derives identity from
`path.stem` any more. Everything per-file (append check, shrink, zero-lines
repair, `transcript_lines`) is unchanged.

**Tech Stack:** Python 3.14, psycopg 3, Postgres 18.6, Typer, pytest,
ruff, pyrefly.

**Spec:** `docs/superpowers/specs/2026-09-16-subagent-transcripts-design.md`
(amends `docs/superpowers/specs/2026-09-15-transcript-capture-design.md`).
Read the spec before starting any task.

## Global Constraints

- Work in `/Users/brandon/llmworkspace/saddlebag/.claude/worktrees/transcript-capture`, branch `transcript-capture`. Do not `cd` elsewhere.
- Never edit an applied migration. 020-023 are applied to the real database; the new one is `024_transcript_agent_id.sql`.
- The constraint being dropped is named exactly `transcripts_owner_id_harness_session_id_key` (read from the live database). The new one is named exactly `transcripts_identity`.
- Verification is `make check` **and** `uv run pyrefly check --output-format=min-text tests/`. In this worktree `make check` silently skips `tests/` for pyrefly. Both must be clean.
- A green pytest run means nothing unless the skip count is zero. Confirm `docker compose ps` shows the database up.
- Never hand-edit anything ruff fixes. Never run a bare `ruff format .` (it rewrites code blocks in `docs/`).
- Prose and comments: spaced hyphens ` - `, never em dashes. Comments explain *why*, at the density of the surrounding code.
- Test fixtures spell Claude Code's layout literally (`"subagents"`, `"agent-"`) - never build them from the code's own constants, or renaming the constant leaves the suite green.
- `transcript_files()` must never call `Path.stat()` on a `.jsonl` file - `test_the_cap_bounds_a_refresh_before_it_reads` counts those calls.
- Run the new CLI as `uv run bag ...` from the worktree; the `bag` on PATH is the main checkout.
- Guard-reversion steps happen AFTER the task's commit, so `git checkout -- <file>` restores the committed code. Clear `__pycache__` before every reverted run: `find src tests -name __pycache__ -type d -exec rm -rf {} +`.

---

## File Structure

- `src/saddlebag/services/transcripts.py` - gains `TranscriptFile` and `transcript_files()`; `run`, `discover`, `status`, `advisories` route through them.
- `src/saddlebag/backends/postgres/migrations/024_transcript_agent_id.sql` - new.
- `src/saddlebag/domain.py` - `Transcript.agent_id`.
- `src/saddlebag/store.py` - `put_transcript` / `get_transcript` take `agent_id`.
- `src/saddlebag/backends/postgres/store.py` - column list, row mapping, upsert target, lookup.
- `src/saddlebag/cli.py` - `discover`, `designate`, `status` rendering.
- `tests/transcript_tree.py` - new. The one fixture builder for the real nested layout.
- `tests/test_transcript_layout.py` - new, no `db` marker.
- `tests/test_transcript_identity_migration.py` - new, `db`.
- `tests/test_store_transcripts.py`, `tests/test_transcripts_import.py`, `tests/test_transcripts_status.py`, `tests/test_transcripts_discover.py`, `tests/test_transcripts_cli.py`, `tests/test_transcript_contract.py` - extended.
- `CLAUDE.md` - the "Session transcripts" section.

---

### Task 1: The layout has one owner

**Files:**
- Create: `tests/transcript_tree.py`
- Create: `tests/test_transcript_layout.py`
- Modify: `src/saddlebag/services/transcripts.py` (add after `transcript_root()`, before `class Candidate`; add names to `__all__`)

**Interfaces:**
- Produces:
  - `transcripts.TranscriptFile` - frozen dataclass `(path: Path, session_id: str, agent_id: str | None)`
  - `transcripts.transcript_files(directory: Path) -> list[TranscriptFile]`
  - `tests.transcript_tree.write_session(directory: Path, session_id: str, lines: list[dict[str, Any]] | None = None) -> Path`
  - `tests.transcript_tree.write_subagent(directory: Path, session_id: str, agent_id: str, lines: list[dict[str, Any]] | None = None) -> Path`
  - `tests.transcript_tree.write_tool_result(directory: Path, session_id: str) -> Path`

- [ ] **Step 1: Write the fixture builder**

Create `tests/transcript_tree.py`:

```python
"""Claude Code's real transcript layout, built for tests.

The first transcript fixtures only ever wrote a flat directory of
`<session-id>.jsonl` files - the same assumption the code made, which is
why no test noticed the import never read a subagent's transcript. A
fixture that mirrors only what the author already believes cannot
falsify the belief. So the nested shape lives here, once, and every
transcript test builds from it.

The layout is spelled LITERALLY - "subagents", "agent-" - and never taken
from the service's constants: this is Claude Code's contract, and a
fixture built from our own names would stay green through a rename that
broke the real thing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _dump(target: Path, lines: list[dict[str, Any]]) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"".join(json.dumps(line).encode() + b"\n" for line in lines))
    return target


def write_session(
    directory: Path, session_id: str, lines: list[dict[str, Any]] | None = None
) -> Path:
    """`<directory>/<session_id>.jsonl` - a session's own transcript."""
    if lines is None:
        lines = [{"type": "user", "sessionId": session_id}]
    return _dump(directory / f"{session_id}.jsonl", lines)


def write_subagent(
    directory: Path,
    session_id: str,
    agent_id: str,
    lines: list[dict[str, Any]] | None = None,
) -> Path:
    """`<directory>/<session_id>/subagents/agent-<agent_id>.jsonl`.

    The default lines carry what real ones do: the PARENT's `sessionId`,
    the file's own `agentId`, and `isSidechain: true`.
    """
    if lines is None:
        lines = [
            {
                "type": "user",
                "sessionId": session_id,
                "agentId": agent_id,
                "isSidechain": True,
            }
        ]
    return _dump(directory / session_id / "subagents" / f"agent-{agent_id}.jsonl", lines)


def write_tool_result(directory: Path, session_id: str) -> Path:
    """`<directory>/<session_id>/tool-results/hook-...-stdout.txt`.

    The one other thing Claude Code writes below a session directory. Not a
    transcript; nothing may import it.
    """
    target = directory / session_id / "tool-results" / "hook-0000-stdout.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hook output\n")
    return target
```

- [ ] **Step 2: Write the failing layout tests**

Create `tests/test_transcript_layout.py` (no `db` marker - it touches only the filesystem):

```python
"""Which files are transcripts, and whose. Filesystem only - runs on CI."""

from __future__ import annotations

from pathlib import Path

from saddlebag.services import transcripts
from tests.transcript_tree import write_session, write_subagent, write_tool_result


def _ids(directory: Path) -> list[tuple[str, str | None]]:
    return [(f.session_id, f.agent_id) for f in transcripts.transcript_files(directory)]


def test_a_session_file_is_its_own_session_with_no_agent(tmp_path: Path) -> None:
    write_session(tmp_path, "s1")
    assert _ids(tmp_path) == [("s1", None)]


def test_a_subagent_file_belongs_to_the_session_directory_above_it(
    tmp_path: Path,
) -> None:
    """The session id comes from the grandparent directory and the agent id
    from the stem minus `agent-` - which is exactly what every line inside
    a real subagent file says, measured across all 392 on this machine."""
    write_session(tmp_path, "s1")
    path = write_subagent(tmp_path, "s1", "aimpl-task2-ffa7dc6fa2964ee8")
    files = transcripts.transcript_files(tmp_path)
    assert [(f.session_id, f.agent_id) for f in files] == [
        ("s1", None),
        ("s1", "aimpl-task2-ffa7dc6fa2964ee8"),
    ]
    assert files[1].path == path


def test_a_parent_precedes_its_own_subagents(tmp_path: Path) -> None:
    """Path ordering compares parts, so `s1` sorts before `s1.jsonl` and a
    naive `sorted()` would put every subagent ahead of its own parent."""
    write_subagent(tmp_path, "s1", "b")
    write_subagent(tmp_path, "s1", "a")
    write_session(tmp_path, "s1")
    write_session(tmp_path, "s0")
    assert _ids(tmp_path) == [("s0", None), ("s1", None), ("s1", "a"), ("s1", "b")]


def test_one_agent_id_under_two_sessions_is_two_files(tmp_path: Path) -> None:
    """Real: four agent ids on this machine appear under two parents with
    different content. The agent id alone is not an identity."""
    write_subagent(tmp_path, "s1", "aimpl-task3")
    write_subagent(tmp_path, "s2", "aimpl-task3")
    assert _ids(tmp_path) == [("s1", "aimpl-task3"), ("s2", "aimpl-task3")]


def test_a_subagent_whose_parent_file_is_missing_is_still_listed(
    tmp_path: Path,
) -> None:
    """The bytes are the scarce thing. A parent Claude Code has deleted does
    not make its subagents' conversations any less worth keeping."""
    write_subagent(tmp_path, "gone", "a1")
    assert _ids(tmp_path) == [("gone", "a1")]


def test_tool_results_and_memory_are_not_transcripts(tmp_path: Path) -> None:
    write_session(tmp_path, "s1")
    write_tool_result(tmp_path, "s1")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "MEMORY.md").write_text("- x\n")
    assert _ids(tmp_path) == [("s1", None)]


def test_a_missing_directory_has_no_transcripts(tmp_path: Path) -> None:
    assert transcripts.transcript_files(tmp_path / "absent") == []
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_transcript_layout.py -q`
Expected: FAIL - `AttributeError: module 'saddlebag.services.transcripts' has no attribute 'transcript_files'`

- [ ] **Step 4: Implement `TranscriptFile` and `transcript_files`**

In `src/saddlebag/services/transcripts.py`, add `"TranscriptFile"` and `"transcript_files"` to `__all__` (ruff sorts it), and insert directly after `transcript_root()`:

```python
#: Claude Code's layout below a session directory. Module constants so the
#: one function that reads the layout names it once - tests deliberately do
#: NOT import these, and spell the layout literally instead.
SUBAGENT_DIR = "subagents"
SUBAGENT_PREFIX = "agent-"


@dataclass(frozen=True)
class TranscriptFile:
    """One transcript on disk, and the identity its PATH gives it.

    Identity is read from the path and never from the contents: a refresh
    has to decide what a file is from a stat, before it reads a byte.
    Measured 2026-09-16, every line's `sessionId` and `agentId` agreed with
    the path in all 392 subagent files on this machine.
    """

    path: Path
    #: For a subagent file this is the PARENT's session id - which is what
    #: the file's own lines say, and what `events` records its tool calls
    #: under.
    session_id: str
    #: None for a session's own transcript; the `agentId` for a subagent's.
    agent_id: str | None


def transcript_files(directory: Path) -> list[TranscriptFile]:
    """Every transcript in a claimed directory, sessions and subagents both.

    The ONLY place that knows the layout. The first version of this feature
    globbed `*.jsonl` in three places, none of them recursive, and all
    three were blind to `<session>/subagents/agent-<id>.jsonl` - more bytes
    than the sessions themselves, and the only copy of those conversations
    (the parent transcript holds none of their lines). One owner is what
    stops a fourth caller reintroducing the blind spot.

    `tool-results/` sits beside `subagents/` and is hook stdout, not a
    transcript, so the subagent pattern is anchored on its directory name
    rather than recursing.

    Sorted on identity, not on the Path: `Path` ordering compares parts, so
    `s1` sorts before `s1.jsonl` and every subagent would come ahead of its
    own parent. Nothing here stats a file - a bounded refresh counts those.
    """
    found = [TranscriptFile(p, p.stem, None) for p in directory.glob("*.jsonl")]
    for p in directory.glob(f"*/{SUBAGENT_DIR}/{SUBAGENT_PREFIX}*.jsonl"):
        found.append(
            TranscriptFile(
                p, p.parent.parent.name, p.stem.removeprefix(SUBAGENT_PREFIX)
            )
        )
    found.sort(key=lambda f: (f.session_id, f.agent_id is not None, f.agent_id or ""))
    return found
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_transcript_layout.py -q`
Expected: 7 passed

- [ ] **Step 6: Run the gates**

Run: `make check` then `uv run pyrefly check --output-format=min-text tests/`
Expected: both clean, 0 skipped.

- [ ] **Step 7: Commit**

```bash
git add tests/transcript_tree.py tests/test_transcript_layout.py src/saddlebag/services/transcripts.py
git commit -m "Give the transcript layout one owner, subagents included"
```

- [ ] **Step 8: Watch the ordering guard fail**

Replace the `found.sort(...)` line with `found.sort(key=lambda f: f.path)`, clear `__pycache__`, run `uv run pytest tests/test_transcript_layout.py -q -p no:cacheprovider`.
Expected: `test_a_parent_precedes_its_own_subagents` FAILS.
Restore: `git checkout -- src/saddlebag/services/transcripts.py`.

---

### Task 2: Identity includes the agent

**Files:**
- Create: `src/saddlebag/backends/postgres/migrations/024_transcript_agent_id.sql`
- Create: `tests/test_transcript_identity_migration.py`
- Modify: `src/saddlebag/domain.py` (`class Transcript`)
- Modify: `src/saddlebag/store.py` (`put_transcript`, `get_transcript` in the Protocol)
- Modify: `src/saddlebag/backends/postgres/store.py` (`transcript_columns`, `_row_to_transcript`, `put_transcript`, `get_transcript`)
- Test: `tests/test_store_transcripts.py`

**Interfaces:**
- Produces:
  - `Transcript.agent_id: str | None` (field directly after `session_id`)
  - `Store.put_transcript(owner_id, project, harness, session_id, path, content, sha256, agent_id: str | None = None) -> Transcript`
  - `Store.get_transcript(owner_id, harness, session_id, agent_id: str | None = None) -> Transcript | None`
  - `stored_transcripts` rows now carry `agent_id` (signature unchanged)

- [ ] **Step 1: Write the failing store tests**

Append to `tests/test_store_transcripts.py`:

```python
def test_a_session_and_its_subagent_are_two_rows(
    store: PostgresStore, owner: Principal
) -> None:
    """A subagent file carries its parent's session id. Without `agent_id`
    in the identity, storing it would overwrite the parent's bytes."""
    parent = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/x/s1.jsonl", b"parent\n",
        sha256_hex(b"parent\n"),
    )
    child = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/x/s1/subagents/agent-a1.jsonl",
        b"child\n", sha256_hex(b"child\n"), agent_id="a1",
    )
    assert parent.id != child.id
    assert parent.agent_id is None
    assert child.agent_id == "a1"
    assert found(store.get_transcript(owner.id, "claude-code", "s1")).id == parent.id
    assert (
        found(store.get_transcript(owner.id, "claude-code", "s1", "a1")).id
        == child.id
    )
    assert store.transcript_content(parent.id, owner.id) == b"parent\n"


def test_a_subagent_put_twice_is_one_row(
    store: PostgresStore, owner: Principal
) -> None:
    first = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/x", b"a\n", sha256_hex(b"a\n"),
        agent_id="a1",
    )
    second = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/x", b"a\nb\n", sha256_hex(b"a\nb\n"),
        agent_id="a1",
    )
    assert first.id == second.id
    assert second.bytes == 4


def test_one_agent_id_under_two_sessions_is_two_rows(
    store: PostgresStore, owner: Principal
) -> None:
    """Real: four agent ids on this machine repeat across parents."""
    one = store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/x", b"a\n", sha256_hex(b"a\n"),
        agent_id="a1",
    )
    two = store.put_transcript(
        owner.id, "p", "claude-code", "s2", "/y", b"b\n", sha256_hex(b"b\n"),
        agent_id="a1",
    )
    assert one.id != two.id


def test_a_second_session_row_with_no_agent_is_refused(
    store: PostgresStore, owner: Principal, conn: psycopg.Connection[Any]
) -> None:
    """`nulls not distinct` is load-bearing. Without it, two NULL-agent rows
    for one session are both admitted and the session's own transcript
    silently loses its uniqueness. Raw SQL, because `put_transcript` would
    upsert and never show the constraint at all."""
    store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/x", b"a\n", sha256_hex(b"a\n")
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "insert into transcripts"
            " (id, owner_id, project, harness, session_id, path,"
            "  content, bytes, sha256)"
            " values (%s, %s, 'p', 'claude-code', 's1', '/x', %s, 1, 'h')",
            (new_id(), owner.id, b"b"),
        )


def test_stored_transcripts_carry_the_agent(
    store: PostgresStore, owner: Principal
) -> None:
    store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/x", b"a\n", sha256_hex(b"a\n")
    )
    store.put_transcript(
        owner.id, "p", "claude-code", "s1", "/y", b"b\n", sha256_hex(b"b\n"),
        agent_id="a1",
    )
    got = {(t.session_id, t.agent_id) for t in store.stored_transcripts(owner.id, "p")}
    assert got == {("s1", None), ("s1", "a1")}
```

- [ ] **Step 2: Write the failing mid-sequence migration test**

Create `tests/test_transcript_identity_migration.py`:

```python
"""Migration 024 over rows written before it.

The real database held 112 transcripts when 024 was written. The question
this answers is what those rows become: session transcripts (a NULL
`agent_id`), still unique per session, with room beside them for their
subagents.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest

from tests.test_vocabulary_migration import apply_rest, apply_through

pytestmark = pytest.mark.db

BEFORE = "023_transcript_runs"

_INSERT = (
    "insert into transcripts"
    " (id, owner_id, project, harness, session_id, path, content, bytes, sha256)"
    " values (%s, %s, 'p', 'claude-code', 's1', '/x', %s, 1, 'h')"
)


def test_rows_written_before_024_become_session_transcripts(
    conn: psycopg.Connection[Any],
) -> None:
    apply_through(conn, BEFORE)
    owner = uuid4()
    conn.execute(
        "insert into principals (id, handle, kind) values (%s, 'pre', 'user')",
        (owner,),
    )
    existing = uuid4()
    conn.execute(_INSERT, (existing, owner, b"a"))

    apply_rest(conn)

    row = conn.execute(
        "select agent_id from transcripts where id = %s", (existing,)
    ).fetchone()
    assert row is not None
    assert row[0] is None

    # Room for its subagent...
    conn.execute(
        "insert into transcripts"
        " (id, owner_id, project, harness, session_id, agent_id, path,"
        "  content, bytes, sha256)"
        " values (%s, %s, 'p', 'claude-code', 's1', 'a1', '/y', %s, 1, 'h')",
        (uuid4(), owner, b"b"),
    )
    # ...and none for a second copy of the session itself.
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(_INSERT, (uuid4(), owner, b"c"))
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_store_transcripts.py tests/test_transcript_identity_migration.py -q`
Expected: FAIL - `put_transcript() got an unexpected keyword argument 'agent_id'` and `column "agent_id" does not exist`. Check the skip count is 0.

- [ ] **Step 4: Write migration 024**

Create `src/saddlebag/backends/postgres/migrations/024_transcript_agent_id.sql`:

```sql
-- Subagent transcripts. Claude Code writes, beside each session's own
-- `<session-id>.jsonl`, one `<session-id>/subagents/agent-<agent-id>.jsonl`
-- per subagent that session dispatched. Every line in one carries the
-- PARENT's `sessionId`, so `(owner_id, harness, session_id)` cannot tell a
-- subagent from its parent - storing one would overwrite the other.
--
-- They are worth the column. Measured 2026-09-16: 392 files, 131MB, and
-- the only copy - the parent transcript holds none of their lines.
--
-- NULL means a session's own transcript, so every row written before this
-- migration is already correct as it stands. `agent_id` alone is not an
-- identity: four agent ids on the machine this was written on repeat under
-- different parents with different content.
alter table transcripts add column agent_id text;

-- Postgres's generated name for 020's unnamed unique constraint, read from
-- the live database rather than guessed.
alter table transcripts
  drop constraint transcripts_owner_id_harness_session_id_key;

-- `nulls not distinct` is the load-bearing half. Under the default, two
-- rows with a NULL agent_id never conflict, so a session's own transcript
-- would silently stop being unique the moment this column appeared.
-- Named, unlike 020's, because `put_transcript` targets it by name.
alter table transcripts
  add constraint transcripts_identity
  unique nulls not distinct (owner_id, harness, session_id, agent_id);
```

- [ ] **Step 5: Add `agent_id` to the domain**

In `src/saddlebag/domain.py`, `class Transcript`, insert directly after `session_id: str`:

```python
    #: None for a session's own transcript; the subagent's `agentId` for a
    #: `<session>/subagents/agent-<id>.jsonl`. `session_id` is the parent's
    #: in both cases.
    agent_id: str | None
```

- [ ] **Step 6: Widen the Protocol**

In `src/saddlebag/store.py`, replace the `put_transcript` comment and the two signatures:

```python
    # transcripts
    #: Upsert by (owner, harness, session, agent). Re-importing a file updates
    #: it rather than creating a twin - identity is the session and agent, not
    #: the path. `agent_id` None is the session's own transcript.
    def put_transcript(
        self,
        owner_id: UUID,
        project: str,
        harness: str,
        session_id: str,
        path: str,
        content: bytes,
        sha256: str,
        agent_id: str | None = None,
    ) -> Transcript: ...
    def get_transcript(
        self,
        owner_id: UUID,
        harness: str,
        session_id: str,
        agent_id: str | None = None,
    ) -> Transcript | None: ...
```

- [ ] **Step 7: Implement in the Postgres store**

In `src/saddlebag/backends/postgres/store.py`:

In `transcript_columns`, add `"agent_id",` directly after `"session_id",`.

In `_row_to_transcript`, add `agent_id=row["agent_id"],` directly after `session_id=row["session_id"],`.

Replace `put_transcript`'s signature, column list, conflict target and parameters (keep the existing `project` comment exactly as it is):

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
        agent_id: str | None = None,
    ) -> Transcript:
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                insert into transcripts
                  (id, owner_id, project, harness, session_id, agent_id, path,
                   content, bytes, sha256)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                -- By constraint name, not by column list: the target has a
                -- nullable column, and naming the constraint says exactly
                -- which uniqueness rule this upsert rides on.
                on conflict on constraint transcripts_identity do update
```

…keeping the `set` clause and its comment unchanged, and the parameter tuple becomes:

```python
                (
                    new_id(),
                    owner_id,
                    project,
                    harness,
                    session_id,
                    agent_id,
                    path,
                    content,
                    len(content),
                    sha256,
                ),
```

Replace `get_transcript`:

```python
    def get_transcript(
        self,
        owner_id: UUID,
        harness: str,
        session_id: str,
        agent_id: str | None = None,
    ) -> Transcript | None:
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select {transcript_columns("t")} from transcripts t
                 where t.owner_id = %s and t.harness = %s and t.session_id = %s
                   -- `=` never matches NULL, and NULL is how a session's own
                   -- transcript is spelled.
                   and t.agent_id is not distinct from %s
                """),
                (owner_id, harness, session_id, agent_id),
            )
            row = cur.fetchone()
            return _row_to_transcript(row) if row else None
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_store_transcripts.py tests/test_transcript_identity_migration.py -q`
Expected: all pass, 0 skipped.

- [ ] **Step 9: Run the gates**

Run: `make check` then `uv run pyrefly check --output-format=min-text tests/`
Expected: both clean, 0 skipped. `test_transcripts_import.py` still passes: every existing call omits `agent_id`, which means "the session's own".

- [ ] **Step 10: Commit**

```bash
git add src/saddlebag/backends/postgres/migrations/024_transcript_agent_id.sql src/saddlebag/domain.py src/saddlebag/store.py src/saddlebag/backends/postgres/store.py tests/test_store_transcripts.py tests/test_transcript_identity_migration.py
git commit -m "Key transcripts on session and agent"
```

- [ ] **Step 11: Watch the `nulls not distinct` guard fail**

In `024_transcript_agent_id.sql`, delete the words `nulls not distinct`. Clear `__pycache__`. Run:
`uv run pytest tests/test_store_transcripts.py::test_a_second_session_row_with_no_agent_is_refused tests/test_transcript_identity_migration.py -q -p no:cacheprovider`
Expected: both FAIL (no `UniqueViolation`).
Restore: `git checkout -- src/saddlebag/backends/postgres/migrations/024_transcript_agent_id.sql`.

Note: the `db_dsn` scratch database is migrated inside each test's rolled-back transaction, so the edited SQL really is what runs. If the tests still pass with the edit, check whether that database was left migrated by something that committed, and say so rather than accepting the result.

---

### Task 3: The import reads subagents

**Files:**
- Modify: `src/saddlebag/services/transcripts.py` (`_run_body`, `_import_one`, `_check_project_agreement`, `_store_whole`, `advisories`)
- Modify: `tests/test_transcripts_cli.py` (`boom` stub signature)
- Test: `tests/test_transcripts_import.py`, `tests/test_transcripts_status.py`

**Interfaces:**
- Consumes: `TranscriptFile`, `transcript_files()` (Task 1); `put_transcript(..., agent_id=)`, `get_transcript(..., agent_id)` (Task 2)
- Produces: `PROJECT_CONFLICT` anomaly entries gain keys `"session_id": str` and `"agent_id": str | None`. `advisories()` phrases conflicts as `"{n} session(s) recorded under another project"`.

- [ ] **Step 1: Fix the CLI crash-test stub first**

In `tests/test_transcripts_cli.py`, the `boom` stub inside `test_import_records_a_crashed_run_rather_than_losing_it` has a fixed four-argument signature. Once the import passes `agent_id`, it would raise `TypeError` before its SQL runs - still an exception, so the test would stay green while no longer proving that a *failed statement* leaves the run row finishable. Change it to:

```python
    def boom(
        self: PostgresStore,
        owner_id: object,
        harness: object,
        session_id: object,
        agent_id: object = None,
    ) -> None:
```

- [ ] **Step 2: Write the failing import tests**

In `tests/test_transcripts_import.py`, add to the imports:

```python
from tests.transcript_tree import write_session, write_subagent, write_tool_result
```

Append:

```python
def test_an_import_stores_every_subagent_file_byte_exact(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """The gap this amendment closes: 392 files and 131MB on the machine it
    was measured on, and the only copy of those conversations."""
    write_session(tmp_path, "s1")
    a1 = write_subagent(
        tmp_path, "s1", "a1", [{"type": "user"}, {"type": "assistant"}]
    )
    write_subagent(tmp_path, "s1", "a2")
    transcripts.designate(store, owner.id, "p", tmp_path)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_new == 3
    parent = found(store.get_transcript(owner.id, transcripts.HARNESS, "s1"))
    child = found(store.get_transcript(owner.id, transcripts.HARNESS, "s1", "a1"))
    assert parent.id != child.id
    assert store.transcript_content(child.id, owner.id) == a1.read_bytes()
    assert child.path == str(a1)
    assert store.transcript_line_count(child.id) == 2
    # Identity came from the path's directories, never from its stem.
    assert store.get_transcript(owner.id, transcripts.HARNESS, "agent-a1") is None


def test_one_agent_id_under_two_sessions_is_stored_twice(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    write_subagent(tmp_path, "s1", "a1", [{"type": "user"}])
    write_subagent(tmp_path, "s2", "a1", [{"type": "assistant"}, {"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_new == 2
    one = found(store.get_transcript(owner.id, transcripts.HARNESS, "s1", "a1"))
    two = found(store.get_transcript(owner.id, transcripts.HARNESS, "s2", "a1"))
    assert store.transcript_line_count(one.id) == 1
    assert store.transcript_line_count(two.id) == 2


def test_a_subagent_whose_parent_file_is_missing_is_still_stored(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """Not an anomaly: nothing about the file is suspect. The missing parent
    is what `irrecoverable` reports."""
    write_subagent(tmp_path, "gone", "a1")
    transcripts.designate(store, owner.id, "p", tmp_path)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_new == 1
    assert report.anomalies == []
    assert store.get_transcript(owner.id, transcripts.HARNESS, "gone", "a1")


def test_an_unchanged_subagent_file_is_not_read_again(
    store: PostgresStore,
    owner: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lookup that ignored the agent would find the PARENT's row, see a
    different size, and read the file - so disabling reads is what proves
    each file is matched to its own row."""
    write_session(tmp_path, "s1", [{"type": "user"}, {"type": "assistant"}])
    write_subagent(tmp_path, "s1", "a1")
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    def _must_not_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("an unchanged file must not be read")

    monkeypatch.setattr(Path, "open", _must_not_read)
    monkeypatch.setattr(Path, "read_bytes", _must_not_read)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_seen == 2
    assert report.files_new == 0
    assert report.files_appended == 0
    assert report.files_rebuilt == 0


def test_an_appended_subagent_file_adds_only_its_new_lines(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    path = write_subagent(tmp_path, "s1", "a1", [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    with path.open("ab") as fh:
        fh.write(json.dumps({"type": "assistant"}).encode() + b"\n")
    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_appended == 1
    assert report.lines_written == 1
    child = found(store.get_transcript(owner.id, transcripts.HARNESS, "s1", "a1"))
    assert store.transcript_content(child.id, owner.id) == path.read_bytes()


def test_tool_results_are_not_imported(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    write_session(tmp_path, "s1")
    write_tool_result(tmp_path, "s1")
    transcripts.designate(store, owner.id, "p", tmp_path)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_seen == 1


def test_a_subagent_of_a_session_recorded_elsewhere_is_an_anomaly_too(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """Looking the session up by the file's stem would find `agent-a1` in
    no recorded project and skip the check for every subagent, silently."""
    _record(store, owner, "B", "s1")
    write_session(tmp_path, "s1")
    write_subagent(tmp_path, "s1", "a1")
    write_subagent(tmp_path, "s1", "a2")
    transcripts.designate(store, owner.id, "A", tmp_path)

    report = transcripts.run(store, owner.id, "A", trigger=TranscriptTrigger.MANUAL)

    assert report.files_new == 3
    assert {(a["session_id"], a["agent_id"]) for a in report.anomalies} == {
        ("s1", None),
        ("s1", "a1"),
        ("s1", "a2"),
    }
    assert all(a["reason"] == transcripts.PROJECT_CONFLICT for a in report.anomalies)
```

In `tests/test_transcripts_status.py`, add the import `from tests.transcript_tree import write_session, write_subagent` and append:

```python
def test_advisories_count_a_conflicting_session_once(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """One session with many subagents is one thing to go and look at.
    Per-file entries stay in the run row; the line counts sessions."""
    record_event_for(store, owner, project="B", session_id="s1")
    write_session(tmp_path, "s1")
    for agent in ("a1", "a2", "a3"):
        write_subagent(tmp_path, "s1", agent)
    transcripts.designate(store, owner.id, "A", tmp_path)
    transcripts.run(store, owner.id, "A", trigger=TranscriptTrigger.MANUAL)

    lines = transcripts.advisories(store, owner.id)

    assert len(lines) == 1
    assert "1 session(s) recorded under another project" in lines[0]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_transcripts_import.py tests/test_transcripts_status.py -q`
Expected: the new tests FAIL (subagent files never seen: `files_new == 1`, `KeyError: 'session_id'`, advisory text mismatch). 0 skipped.

- [ ] **Step 4: Route the import through `transcript_files`**

In `src/saddlebag/services/transcripts.py`:

In `_run_body`, replace the inner loop:

```python
        for file in transcript_files(directory):
            if budget is not None and budget <= 0:
                return
            report.files_seen += 1
            did_work = _import_one(store, owner_id, project, file, report, recorded)
            if did_work and budget is not None:
                budget -= 1
```

Replace the head of `_import_one` (signature through the `existing is None` branch; the rest of the body is unchanged and keeps using the local `path`):

```python
def _import_one(
    store: Store,
    owner_id: UUID,
    project: str,
    file: TranscriptFile,
    report: Report,
    recorded: dict[str, set[str]],
) -> bool:
    """Import or update one transcript. True when it did any reading.

    The budget is spent only on files that were actually read, so a refresh
    over a directory of unchanged transcripts costs one stat each and skips
    nothing it could have done.

    Identity arrives on `file` and is never re-derived from `path.stem`:
    for a subagent the stem is `agent-<id>`, which is neither the session
    nor, alone, the agent - and both places that once read the stem would
    have gone wrong without raising.
    """
    path = file.path
    existing = store.get_transcript(owner_id, HARNESS, file.session_id, file.agent_id)

    try:
        disk_size = path.stat().st_size
    except OSError as exc:
        report.failures.append({"path": str(path), "reason": str(exc)})
        return False

    _check_project_agreement(project, file, report, recorded)

    if existing is None:
        return _store_whole(store, owner_id, project, file, report, new=True)
```

In the same function, the two later `_store_whole(store, owner_id, project, path, report, new=False)` calls become `_store_whole(store, owner_id, project, file, report, new=False)`. `_plan_for(existing, path, ...)` and `_append(store, owner_id, existing, path, report)` keep taking `path`.

Replace `_check_project_agreement`'s signature and body below its docstring (append one paragraph to the docstring):

```python
def _check_project_agreement(
    project: str,
    file: TranscriptFile,
    report: Report,
    recorded: dict[str, set[str]],
) -> None:
    """...existing docstring...

    Subagent files are checked against their PARENT's session id, which is
    what `events` records their tool calls under, and each gets its own
    entry - each really is a file stored under a disputed label. The entry
    names the session and agent so `advisories` can count sessions rather
    than files.
    """
    known = recorded.get(file.session_id)
    if not known or project in known:
        return
    report.anomalies.append(
        {
            "reason": PROJECT_CONFLICT,
            "path": str(file.path),
            "session_id": file.session_id,
            "agent_id": file.agent_id,
            "claiming": project,
            "recorded": sorted(known),
        }
    )
```

Replace `_store_whole`'s signature and the read/put:

```python
def _store_whole(
    store: Store,
    owner_id: UUID,
    project: str,
    file: TranscriptFile,
    report: Report,
    *,
    new: bool,
) -> bool:
    path = file.path
    try:
        content = path.read_bytes()
    except OSError as exc:
        report.failures.append({"path": str(path), "reason": str(exc)})
        return False

    stored = store.put_transcript(
        owner_id,
        project,
        HARNESS,
        file.session_id,
        str(path),
        content,
        sha256_hex(content),
        agent_id=file.agent_id,
    )
```

(the rest of `_store_whole` is unchanged).

- [ ] **Step 5: Count conflicting sessions in `advisories`**

In `advisories`, replace the `conflicts = ...` statement and the `other`/parts lines:

```python
            conflict_entries = [
                a for a in run.anomalies if a.get("reason") == PROJECT_CONFLICT
            ]
            # Sessions, not files: one session with 57 subagent files is one
            # thing to go and look at. An entry written before anomalies
            # named their session falls back to its path, so it is still
            # counted rather than collapsed into a phantom None session.
            conflicts = len(
                {a.get("session_id") or a.get("path") for a in conflict_entries}
            )
            parts = []
            if shrank:
                parts.append(f"{shrank} transcript(s) shrank on disk")
            if conflicts:
                parts.append(f"{conflicts} session(s) recorded under another project")
            other = len(run.anomalies) - shrank - len(conflict_entries)
```

Then `grep -rn "recorded under another project" tests src` and update any existing assertion to the new wording.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_transcripts_import.py tests/test_transcripts_status.py tests/test_transcripts_cli.py -q`
Expected: all pass, 0 skipped.

- [ ] **Step 7: Run the gates**

Run: `make check` then `uv run pyrefly check --output-format=min-text tests/`
Expected: both clean, 0 skipped.

- [ ] **Step 8: Commit**

```bash
git add src/saddlebag/services/transcripts.py tests/test_transcripts_import.py tests/test_transcripts_status.py tests/test_transcripts_cli.py
git commit -m "Import subagent transcripts beside their sessions"
```

- [ ] **Step 9: Watch each guard fail, one at a time**

For each variant: make the edit, clear `__pycache__`, run
`uv run pytest tests/test_transcripts_import.py tests/test_transcripts_status.py -q -p no:cacheprovider`,
confirm the named test FAILS, then `git checkout -- src/saddlebag/services/transcripts.py`.

1. In `_run_body`, iterate `[TranscriptFile(p, p.stem, None) for p in sorted(directory.glob("*.jsonl"))]` instead of `transcript_files(directory)` -> `test_an_import_stores_every_subagent_file_byte_exact` fails.
2. In `_store_whole`, pass `path.stem` instead of `file.session_id` -> `test_an_import_stores_every_subagent_file_byte_exact` fails.
3. In `_check_project_agreement`, use `recorded.get(file.path.stem)` -> `test_a_subagent_of_a_session_recorded_elsewhere_is_an_anomaly_too` fails.
4. In `_import_one`, drop `file.agent_id` from the `get_transcript` call -> `test_an_unchanged_subagent_file_is_not_read_again` fails.
5. In `advisories`, set `conflicts = len(conflict_entries)` -> `test_advisories_count_a_conflicting_session_once` fails.

Report each variant's result. A variant that stays green is a finding, not a pass.

---

### Task 4: Status, discover and designate say what they cover

**Files:**
- Modify: `src/saddlebag/services/transcripts.py` (`Candidate`, `discover`, `PathStatus`, `TranscriptStatus`, `status`, `status_to_dict`)
- Modify: `src/saddlebag/cli.py` (`transcripts_discover`, `transcripts_designate`, `transcripts_status`)
- Test: `tests/test_transcripts_status.py`, `tests/test_transcripts_discover.py`, `tests/test_transcripts_cli.py`

**Interfaces:**
- Consumes: `transcript_files()` (Task 1); `Transcript.agent_id` (Task 2)
- Produces:
  - `Candidate.subagents: int` (after `total`)
  - `PathStatus.subagents: int` (after `on_disk`; `on_disk` now counts session files only)
  - `TranscriptStatus.subagent_backlog: int` (after `backlog`; `backlog` counts session files only)
  - `status_to_dict` keys: top level `{"project", "paths", "run", "backlog", "subagent_backlog", "irrecoverable"}`; each path `{"path", "present", "on_disk", "subagents"}`

- [ ] **Step 1: Write the failing service tests**

In `tests/test_transcripts_status.py`, change the key assertion in `test_status_of_an_unclaimed_project_is_a_full_document` to:

```python
    assert set(got) == {
        "project",
        "paths",
        "run",
        "backlog",
        "subagent_backlog",
        "irrecoverable",
    }
```

Append:

```python
def test_status_counts_subagent_files_apart_from_sessions(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """"Clean, backlog 0" while half the bytes sat on disk is how this gap
    hid. Subagent files get their own figures so it cannot hide again."""
    claimed = tmp_path / "claimed"
    write_session(claimed, "s1")
    write_subagent(claimed, "s1", "a1")
    write_subagent(claimed, "s1", "a2")
    transcripts.designate(store, owner.id, "p", claimed)

    before = transcripts.status(store, owner.id, "p", tmp_path)
    assert (before.paths[0].on_disk, before.paths[0].subagents) == (1, 2)
    assert (before.backlog, before.subagent_backlog) == (1, 2)

    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    after = transcripts.status(store, owner.id, "p", tmp_path)
    assert (after.backlog, after.subagent_backlog) == (0, 0)
    doc = transcripts.status_to_dict(after)
    assert doc["subagent_backlog"] == 0
    assert doc["paths"][0]["subagents"] == 2


def test_a_stored_subagent_does_not_make_its_lost_session_recoverable(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """The session's own transcript is gone from disk and was never stored.
    Its subagent being stored changes nothing about that."""
    record_event_for(store, owner, project="p", session_id="s1")
    claimed = tmp_path / "claimed"
    write_subagent(claimed, "s1", "a1")
    transcripts.designate(store, owner.id, "p", claimed)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    got = transcripts.status(store, owner.id, "p", tmp_path)

    assert got.irrecoverable == 1
```

In `tests/test_transcripts_discover.py`, add `from tests.transcript_tree import write_subagent` and append:

```python
def test_discover_proves_by_sessions_and_names_the_subagents_a_claim_brings(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """Subagent files add no evidence of ownership - they carry their
    parent's session id - but a claim imports them, so they are counted."""
    old = tmp_path / "-dir"
    _transcript(old, "sess-1")
    _transcript(old, "never-recorded")
    for agent in ("a1", "a2", "a3"):
        write_subagent(old, "sess-1", agent)
    record_event_for(store, owner, project="p", session_id="sess-1")

    found = transcripts.discover(store, owner.id, "p", tmp_path)

    assert (found[0].matched, found[0].total, found[0].subagents) == (1, 2, 3)
```

- [ ] **Step 2: Write the failing CLI tests**

In `tests/test_transcripts_cli.py`, at the end of the `cli_env` fixture (after the loop writing the three session files), add:

```python
    # One subagent file, so every command that describes this directory
    # has to say something about the files a claim brings beyond sessions.
    subagent = directory / "sess-1" / "subagents" / "agent-a1.jsonl"
    subagent.parent.mkdir(parents=True)
    subagent.write_bytes(json.dumps({"type": "user"}).encode() + b"\n")
```

and extend the fixture docstring by one sentence saying so. Then:

In `test_discover_prints_its_evidence_and_writes_nothing`, add
`assert "plus 1 subagent files" in result.stdout`.

Replace the body of `test_designate_echoes_the_backfill_command_and_its_cost` after the docstring with:

```python
    directory = tmp_path / ".claude" / "projects" / "-old-dirname"
    result = runner.invoke(app, ["transcripts", "designate", str(directory)])
    assert result.exit_code == 0
    assert "bag transcripts import" in result.stdout
    assert "3 sessions, 1 subagent files" in result.stdout
```

In `test_status_json_has_the_same_keys_when_nothing_is_claimed`, change the key set to include `"subagent_backlog"`.

Append:

```python
def test_status_prints_subagent_figures_beside_session_ones(
    cli_env: None, tmp_path: Path
) -> None:
    directory = tmp_path / ".claude" / "projects" / "-old-dirname"
    assert runner.invoke(app, ["transcripts", "designate", str(directory)]).exit_code == 0
    result = runner.invoke(app, ["transcripts", "status"])
    assert result.exit_code == 0
    assert "3 sessions, 1 subagent files" in result.stdout
    assert "backlog: 3 sessions, 1 subagent files" in result.stdout
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_transcripts_status.py tests/test_transcripts_discover.py tests/test_transcripts_cli.py -q`
Expected: new and edited tests FAIL (`AttributeError: ... 'subagents'`, missing keys, text mismatch). 0 skipped.

- [ ] **Step 4: Implement `discover`**

In `Candidate`, add after `total`:

```python
    #: Subagent files under this directory's sessions. Not evidence - they
    #: carry their parent's session id, so they prove nothing `matched` has
    #: not - but a claim imports them, and `total` exists to say what a
    #: claim commits someone to reading.
    subagents: int
```

In `discover`, replace the per-directory body:

```python
        files = transcript_files(directory)
        sessions = [f for f in files if f.agent_id is None]
        if not sessions:
            continue
        matched = sum(1 for f in sessions if f.session_id in recorded)
        if matched == 0:
            continue
        found.append(
            Candidate(
                path=str(directory),
                matched=matched,
                total=len(sessions),
                subagents=len(files) - len(sessions),
                claimed_by=claims.get(str(directory)),
            )
        )
```

- [ ] **Step 5: Implement `status`**

`PathStatus` gains, after `on_disk: int`:

```python
    #: Subagent files, counted apart from `on_disk` (sessions) - folding
    #: them together is how a status that read "clean" once hid them.
    subagents: int
```

`TranscriptStatus` gains, after `backlog: int`:

```python
    subagent_backlog: int
```

Replace the body of `status` from `claims = ...` to the `return`:

```python
    claims = store.transcript_paths(owner_id, project)
    paths: list[PathStatus] = []
    on_disk: set[tuple[str, str | None]] = set()
    for claim in claims:
        directory = Path(claim.path)
        present = directory.is_dir()
        files = transcript_files(directory) if present else []
        on_disk.update((f.session_id, f.agent_id) for f in files)
        subagents = sum(1 for f in files if f.agent_id is not None)
        paths.append(
            PathStatus(
                path=claim.path,
                present=present,
                on_disk=len(files) - subagents,
                subagents=subagents,
            )
        )

    stored = {
        (t.session_id, t.agent_id)
        for t in store.stored_transcripts(owner_id, project)
    }
    # Sessions whose OWN transcript is stored. A stored subagent does not
    # make its lost parent recoverable, and `recorded` is a set of sessions.
    stored_sessions = {session for session, agent in stored if agent is None}
    recorded = set(store.event_session_ids(owner_id, project))

    # "Anywhere" means anywhere under the transcript root, not only under a
    # claimed directory: a session whose file sits in an unclaimed directory
    # is recoverable by claiming it, and calling that irrecoverable would
    # overstate the loss. Session files only, for the same reason as above.
    everywhere = {f.stem for f in root.glob("*/*.jsonl")} if root.is_dir() else set()

    missing = on_disk - stored
    return TranscriptStatus(
        project=project,
        paths=paths,
        run=store.latest_transcript_run(owner_id, project),
        backlog=sum(1 for _, agent in missing if agent is None),
        subagent_backlog=sum(1 for _, agent in missing if agent is not None),
        irrecoverable=len(recorded - stored_sessions - everywhere),
    )
```

In `status_to_dict`, the path dict becomes
`{"path": p.path, "present": p.present, "on_disk": p.on_disk, "subagents": p.subagents}`
and `"subagent_backlog": got.subagent_backlog,` goes directly after `"backlog"`.

- [ ] **Step 6: Update the CLI rendering**

In `src/saddlebag/cli.py`:

`transcripts_discover`, the loop:

```python
    for c in found:
        claimed = f" - claimed by {c.claimed_by}" if c.claimed_by else ""
        extra = f", plus {c.subagents} subagent files" if c.subagents else ""
        typer.echo(
            f"{c.path}  {c.matched} of {c.total} sessions match{extra}{claimed}"
        )
```

`transcripts_designate`, replace the `count = ...` line and the echo:

```python
    # Through the service's layout function, not a glob: a hand-written
    # `*.jsonl` here is exactly the blind spot that under-reported what a
    # claim brings in.
    files = transcripts_service.transcript_files(Path(absolute))
    subagents = sum(1 for f in files if f.agent_id is not None)
    typer.echo(
        f"{resolved} claims {absolute} ({len(files) - subagents} sessions, "
        f"{subagents} subagent files). Nothing has been imported yet - run "
        f"`bag transcripts import` to read them."
    )
```

`transcripts_status`, the path loop and backlog line:

```python
    for p in got.paths:
        state = (
            f"{p.on_disk} sessions, {p.subagents} subagent files"
            if p.present
            else "missing"
        )
        typer.echo(f"  {p.path}: {state}")
    typer.echo(
        f"  backlog: {got.backlog} sessions, {got.subagent_backlog} subagent files"
    )
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_transcripts_status.py tests/test_transcripts_discover.py tests/test_transcripts_cli.py -q`
Expected: all pass, 0 skipped.

- [ ] **Step 8: Run the gates**

Run: `make check` then `uv run pyrefly check --output-format=min-text tests/`
Expected: both clean, 0 skipped.

- [ ] **Step 9: Commit**

```bash
git add src/saddlebag/services/transcripts.py src/saddlebag/cli.py tests/test_transcripts_status.py tests/test_transcripts_discover.py tests/test_transcripts_cli.py
git commit -m "Report subagent transcripts in status, discover and designate"
```

- [ ] **Step 10: Watch the irrecoverable filter fail**

In `status`, replace `stored_sessions = {session for session, agent in stored if agent is None}` with `stored_sessions = {session for session, _ in stored}`. Clear `__pycache__`. Run:
`uv run pytest tests/test_transcripts_status.py -q -p no:cacheprovider`
Expected: `test_a_stored_subagent_does_not_make_its_lost_session_recoverable` FAILS.
Restore: `git checkout -- src/saddlebag/services/transcripts.py`.

---

### Task 5: Docs and the real-file contract

**Files:**
- Modify: `CLAUDE.md` (section "### Session transcripts")
- Modify: `tests/test_transcript_contract.py`

**Interfaces:**
- Consumes: `transcript_files()` (Task 1)

- [ ] **Step 1: Extend the freshness check to a real subagent file**

In `tests/test_transcript_contract.py`, append:

```python
@pytest.mark.transcript
def test_a_real_subagent_transcript_still_parses_and_names_its_parent() -> None:
    """May skip. The identity `transcript_files` reads from a subagent's
    PATH is only right while the lines inside agree with it - measured true
    for all 392 files on 2026-09-16. This is what notices when Claude Code
    changes that."""
    root = transcript_root()
    found = (
        sorted(root.glob("*/*/subagents/agent-*.jsonl")) if root.is_dir() else []
    )
    if not found:
        pytest.skip(f"no subagent transcript under {root}")
    path = found[-1]
    lines, _ = parse(path.read_bytes())
    assert lines, "a real subagent transcript parsed to no lines at all"
    unknown = {line.type for line in lines if line.type} - KNOWN_TYPES
    assert not unknown, f"new line types: {sorted(unknown)}"
    sessions = {line.raw.get("sessionId") for line in lines} - {None}
    agents = {line.raw.get("agentId") for line in lines} - {None}
    assert sessions == {path.parent.parent.name}
    assert agents == {path.stem.removeprefix("agent-")}
```

Run: `uv run pytest tests/test_transcript_contract.py -q`
Expected: pass (this machine has subagent files).

- [ ] **Step 2: Update CLAUDE.md**

In `CLAUDE.md`, section `### Session transcripts`, insert after the paragraph that ends "...claimable only by a human who knows it exists. That is why `bag transcripts discover` proposes claims with their evidence and writes nothing; `bag transcripts designate <dir>` is the human act that decides.":

```markdown
**A session is more than one file.** Beside `<session id>.jsonl`, Claude
Code writes `<session id>/subagents/agent-<agent id>.jsonl` for every
subagent the session dispatched, and those are the only copy of those
conversations - the parent transcript holds none of their lines. The first
import globbed `*.jsonl` non-recursively in three places and reported
"clean, backlog 0" while leaving more bytes on disk than it stored.
`transcripts.transcript_files()` is now the one owner of the layout, and
nothing may derive identity from `path.stem`: for a subagent the stem is
`agent-<id>`, and both places that once read it went wrong without
raising. Identity is `(session_id, agent_id)` with `agent_id` NULL for the
session's own file (migration 024, `nulls not distinct` - load-bearing, or
the session row stops being unique), and a subagent row's `session_id` is
its parent's, which is what its own lines say and what `events` records
its tool calls under. `agent_id` alone is not an identity; real ones repeat
across parents. Any question about *sessions* - `irrecoverable`, the
`backlog` figure, `discover`'s proof - must filter to `agent_id is null`,
and subagent counts are reported beside session counts rather than folded
into them. `tool-results/`, the other thing below a session directory, is
hook stdout and is not read.
```

- [ ] **Step 3: Run the gates**

Run: `make check` then `uv run pyrefly check --output-format=min-text tests/`
Expected: both clean, 0 skipped. Confirm with `grep -c '—' CLAUDE.md` that no em dash was introduced (compare with `git show HEAD:CLAUDE.md | grep -c '—'` - the existing count must not grow).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md tests/test_transcript_contract.py
git commit -m "Document subagent transcripts and check a real one"
```

---

### Task 6: Rollout against the real database (controller, not a subagent)

This touches the real database and real files. The controller runs it and reports each result to the user; no subagent is dispatched.

- [ ] **Step 1: Back up the transcript tables**

```bash
docker exec saddlebag-db-1 pg_dump -U saddlebag -d saddlebag -Fc -t transcripts -t transcript_lines -t transcript_paths -t transcript_runs -f /tmp/transcripts-pre-024.dump
docker cp saddlebag-db-1:/tmp/transcripts-pre-024.dump /private/tmp/claude-501/-Users-brandon-llmworkspace-saddlebag--claude-worktrees-transcript-capture/1c61f8d1-c8c7-4ebf-ae3e-51657485183f/scratchpad/transcripts-pre-024.dump
```

Confirm the file exists and is non-trivial in size.

- [ ] **Step 2: Record the before-state**

```bash
docker exec saddlebag-db-1 psql -U saddlebag -d saddlebag -Atc "select count(*), sum(bytes) from transcripts"
```

Expected: `112|75542259`.

- [ ] **Step 3: Migrate**

Run: `uv run bag db status` - expected: exactly `024_transcript_agent_id` pending.
Run: `uv run bag db up`.
Run: `uv run bag db status` - expected: nothing pending.

- [ ] **Step 4: Import**

Run from the worktree. The `-remem` claim is filed under project `saddlebag` (read from `transcript_paths` on 2026-09-16):
`uv run bag transcripts import --project saddlebag`
Expected: `files_new` equals the number of `-remem/*/subagents/*.jsonl` files on disk; 0 failures; the 112 session files are neither appended nor rebuilt unless Claude Code wrote to them since.

- [ ] **Step 5: Verify**

- Stored subagent bytes against disk:
  `docker exec saddlebag-db-1 psql -U saddlebag -d saddlebag -Atc "select count(*), sum(bytes) from transcripts where agent_id is not null and path like '%-Users-brandon-llmworkspace-remem/%'"`
  compared with `find ~/.claude/projects/-Users-brandon-llmworkspace-remem -path '*/subagents/*.jsonl' | xargs stat -f %z | paste -sd+ - | bc` and the file count. Equal, or explained by files that grew since.
- `uv run bag transcripts status --project saddlebag`: `backlog: 0 sessions, 0 subagent files`.
- Session rows still 112: `select count(*) from transcripts where agent_id is null`.
- Three subagent transcripts byte-identical: for three ids, `select encode(sha256(content), 'hex') from transcripts where id = ...` against `shasum -a 256 <path>`.

- [ ] **Step 6: Final whole-branch review of this amendment**

Write the diff from the spec commit (`10213cc`) to HEAD to a file and dispatch a reviewer on the most capable model, per the stored rule: its value is what the task reviews could not see; hand it the deferred-minors list; ask it to assess the controller's rulings; require a named verdict file.

- [ ] **Step 7: Hand the merge decision back to the user**

Merging is deploying: the editable install makes the branch's hooks live in every session. Do not merge without the user's go-ahead.
