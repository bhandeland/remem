# Capturing subagent transcripts

Design, 2026-09-16. Amends
[Capturing session transcripts](2026-09-15-transcript-capture-design.md),
on the same branch, before it merges.

## The problem

The transcript design assumed one file per session:
`<root>/<slug>/<session-id>.jsonl`. Claude Code also writes

```
<root>/<slug>/<session-id>/subagents/agent-<agent-id>.jsonl
```

one per subagent the session dispatched, and the import never sees them:
`run`, `discover` and `status` all enumerate with a non-recursive
`glob("*.jsonl")`. The first real import therefore reported "clean, backlog
0" for `-remem` while leaving more bytes on disk than it stored.

Measured 2026-09-16 across the whole transcript root:

- 392 subagent files, 33,690 lines, 131MB, under only 20 parent sessions
  (the largest has 57).
- **They are the only copy.** In all 20 parents, the parent transcript holds
  zero `isSidechain` lines and shares zero line `uuid`s with its subagent
  files. Nothing already stored covers them.
- They are the reasoning-dense half: each file is a whole implementer or
  reviewer conversation, and capturing *why* is this feature's purpose.

So this is in scope, and it lands before the branch merges: merging is
deploying (the editable install), and shipping a pipeline that reports clean
while missing its densest material is the failure this amendment exists to
remove.

## What a subagent file is

Established by reading every file, not by sampling:

- Every line carries `sessionId` equal to the **parent's** session id, and
  `agentId` equal to the filename stem after `agent-`. Zero of 392 files
  disagree with their path on either.
- `agentId` is **not** unique on its own. Four ids appear under two
  different parents with different content (`aimpl-task2-ffa7dc6fa2964ee8`
  is 121 lines under one and 183 under the other, no common prefix). The
  identity of a subagent file is the pair `(session_id, agent_id)`.
- Every subagent directory's parent `.jsonl` exists today. Nothing
  guarantees it will stay that way.
- A subagent directory sits inside its parent's session directory, which
  sits inside the claimed directory. A claim therefore already covers it,
  and it belongs to the claiming project with no further evidence needed.
- `tool-results/`: 95 `.txt` files, 3.9MB, of hook stdout. Not a
  transcript, not JSONL, out of scope.
- `agent-<agent-id>.meta.json` beside each subagent transcript: 428
  files, 107KB, one per transcript (one transcript has none). JSON
  naming the subagent's `agentType`, `description`, `model` and
  sometimes the parent's `toolUseId` - for some subagents the only
  record of what they were for. Missed by the first survey of this
  design, which filtered on the `agent-` prefix and so never saw a
  second suffix. Not captured by this amendment; see "What this does
  not do".

## Data model

### Migration 024

Migration 020 is applied to the real database (112 rows), so it is not
edited.

```sql
alter table transcripts add column agent_id text;

alter table transcripts
  drop constraint transcripts_owner_id_harness_session_id_key;

alter table transcripts
  add constraint transcripts_identity
  unique nulls not distinct (owner_id, harness, session_id, agent_id);
```

- `agent_id` is **NULL for a session's own transcript** and the `agentId`
  for a subagent file. The 112 existing rows become parent rows with no
  backfill.
- `nulls not distinct` is load-bearing. Without it two NULL-`agent_id` rows
  for one session are both admitted and the parent row silently loses its
  uniqueness. Postgres 15+; the server is 18.6.
- The dropped constraint name is Postgres's generated one, read from the
  live database on 2026-09-16, not guessed.
- `put_transcript`'s `on conflict` names the new constraint
  (`on conflict on constraint transcripts_identity`). The old column-list
  target matches no unique index once the old constraint is gone.
- `put_transcript` still does **not** carry `project = excluded.project`.
  That ruling is unchanged.

### `session_id` on a subagent row is the parent's

That is what the file itself says, and it keeps every join to `events`
correct: the project-agreement check, `irrecoverable` and `discover` all
key on it, and a subagent's tool calls are recorded under the parent's
session id.

### No `parent_id`

A foreign key to the parent row would force the parent to be stored first
and would refuse a subagent whose parent file has gone - and the bytes are
the scarce thing. The pair `(session_id, agent_id IS NULL)` already names
the parent.

### What does not change

`transcript_lines` hangs off `transcripts.id`, so a subagent file gets its
own `(transcript_id, seq)` coordinates with no change. The append check,
shrink handling and the zero-lines repair are all per file and apply as
they are. `transcript_paths` and `transcript_runs` are untouched.

### Domain and store

- `Transcript` gains `agent_id: str | None`.
- `get_transcript` and `put_transcript` take `agent_id`.
- `stored_transcripts` returns both kinds; callers filter.

## Enumeration has one owner

```python
@dataclass(frozen=True)
class TranscriptFile:
    path: Path
    session_id: str
    agent_id: str | None

def transcript_files(directory: Path) -> list[TranscriptFile]: ...
```

It returns `<dir>/*.jsonl` (agent `None`) and
`<dir>/*/subagents/agent-*.jsonl` (session from the grandparent directory
name, agent from the stem minus `agent-`), sorted so a parent precedes its
own subagents. Every caller that globbed goes through it - `run`,
`discover`, `status` - so the blind spot cannot come back through one
caller that was missed. The `agent-<id>.meta.json` sidecar is also not
matched, because the pattern is anchored on `.jsonl`.

**Identity is read from the path, never from the contents.** A refresh must
classify a file from a stat, before any read, and the path agreed with the
contents in all 392 files.

The layout is a fact about Claude Code, so this lives in
`services/transcripts.py` beside `HARNESS` and `transcript_root()`.

## Import

`_run_body` iterates `transcript_files()` and passes `session_id` and
`agent_id` down. Two helpers currently derive identity from `path.stem`,
and both would be **silently** wrong for a subagent file:

- `_store_whole` would store `agent-aimpl-task2-...` as a session id.
- `_check_project_agreement` would look `agent-...` up in `recorded`, find
  nothing, and skip the check for every subagent file.

Both take identity as arguments and never read `path.stem`.

- **Report counters count files**, subagent files included. `files_seen`,
  `files_new` and the rest describe I/O, and a subagent file is I/O.
- **`REFRESH_FILE_CAP` stays 25 files read.** The largest session needs
  three session starts to finish; the run is idempotent and the bulk
  backfill is the typed `import`.
- **Project-conflict anomalies stay per file** - each file really is stored
  under a disputed label - and every entry carries `session_id` and
  `agent_id`. `advisories()` counts **distinct sessions**, so a conflicting
  session with 57 subagent files reads as one session, not 58.
- A subagent file whose parent is missing is stored like any other. It is
  not an anomaly: nothing about it is suspect, and its parent's absence is
  what `irrecoverable` reports.

## Status

The blind spot showed up here, so subagent counts get their own fields
rather than being folded into the session ones.

- `PathStatus.on_disk` stays session files; `PathStatus.subagents` is new.
- `backlog` stays sessions only; `subagent_backlog` is new - subagent files
  on disk whose `(session_id, agent_id)` is not stored.
- `irrecoverable` stays session-level: a recorded session with no parent
  row stored and no parent file anywhere under the root. The stored set is
  filtered to `agent_id is None`, because a stored subagent does not make
  its lost parent recoverable. The "anywhere" glob stays `*/*.jsonl`.
- `status_to_dict` carries the new keys in every state, per the existing
  same-keys rule. The CLI prints them beside the session figures.

## Discover

Proof stays session-level: `matched` and `total` count parent files only,
since subagent files add no evidence of ownership. `Candidate` gains
`subagents`, because `total` exists to warn that a claim imports all of a
directory, and subagent files are now part of that.

A directory whose sessions' own `.jsonl` files are all gone, leaving only
subagent files, is never proposed - `matched` counts parent files - and so
can only be claimed by someone who knows it exists, the same floor as a
directory with no recorded sessions.

## Testing

The previous fixtures built only flat directories - the same assumption the
code made, which is why nothing caught this. So:

- **One shared fixture builder writes the real nested layout**, including
  the cases the probe found: one `agent_id` under two parents with
  different content, a `subagents/` directory whose parent `.jsonl` is
  missing, and a `tool-results/` directory that must be ignored.
- **`transcript_files()`** is tested without a `db` marker - it touches only
  the filesystem - so it runs on CI.
- **The constraint, with real SQL** (`db`): a second parent row for one
  session is refused; a parent and a subagent for one session both land;
  one `agent_id` under two sessions lands twice; migration 024 over
  pre-existing rows leaves them as NULL-`agent_id` parents.
- **End to end:** after an import every subagent file is stored byte-exact
  with lines under its own `transcript_id`; `subagent_backlog` reaches 0;
  `irrecoverable` does not count a session whose only stored file is a
  subagent; a conflicting session's advisory says one session; a
  subagent's project-conflict anomaly is raised at all.
- **Each guard is watched failing.** On a scratch copy, revert one at a time
  and confirm a test goes red, clearing `__pycache__` between variants:
  the non-recursive glob, `path.stem` in `_store_whole`, `path.stem` in
  the agreement check, and `nulls not distinct`.
- **Gates:** `make check`, and also
  `uv run pyrefly check --output-format=min-text tests/` - in this worktree
  `make check` silently skips `tests/` because the worktree sits under an
  ignored `.claude/`.

## Rollout against the real database

024 is additive plus a constraint swap and is reversible, but it runs over
112 real rows.

1. `pg_dump` the four transcript tables to the scratchpad (about 31MB).
2. `uv run bag db status`, then `uv run bag db up`.
3. `uv run bag transcripts import` for the `-remem` claim. Expected: the
   112 parent files classify SKIP at one stat each; every subagent file is
   new.
4. Verify: stored subagent bytes equal the on-disk total for
   `-remem/*/subagents/`; `subagent_backlog` 0 and `backlog` still 0; three
   subagent transcripts byte-identical on spot-check; parent row count
   still 112.

Then a whole-branch review of this amendment's diff on the most capable
model, then the merge decision.

## What this does not do

- No `tool-results/` capture, and no capture of `agent-<id>.meta.json`
  sidecars. The sidecars are the stronger candidate - small, irreplaceable,
  and descriptive of the subagent - and are an open decision: storing them
  needs a nullable column on the subagent's row (migration 025) and a
  re-read rule for rows imported before it.
- No parent linkage beyond the shared `session_id`, and no rendering of a
  subagent conversation inline into its parent's.
- No change to discovery's proof, the claim model, redaction, or the
  refresh cap.
