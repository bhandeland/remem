# Capturing session transcripts

Design, 2026-09-15.

## The problem

saddlebag records the tool layer of a session and nothing else. The Claude
Code adapter registers four hooks, of which two record: `PostToolUse` ->
`tool_call` and `SessionEnd` -> `session_end`. `UserPromptSubmit` runs the
session-size reminder and stores nothing; `SessionStart` injects. The data
agrees - `claude-code` has 5,481 `tool_call` rows, 35 `session_end` rows,
and **zero** `message` rows, while both cursor and opencode record messages.

So what is in Postgres is `tool_input`, `tool_response`, `cwd` and a tool
name. None of the user prompts, none of the assistant text, none of the
reasoning between the calls. The part of a session that carries *why*
something was done is not captured by the pipeline whose purpose is to
capture why something was done.

The full trace does exist. Claude Code writes complete JSONL to
`~/.claude/projects/<slug>/<session-id>.jsonl`: 34 transcripts and 22MB for
this project's current directory, 263MB across 36 project directories. Every
`session_end` payload already carries `transcript_path`, populated, for all
35 recorded sessions. We have always known exactly which file belongs to
which session. We have never read one.

### This finishes a decision rather than reversing one

The events design (2026-08-28) replaced transcript-based capture, and stated
its reason as a rule:

> Derived data lives apart from its source, is recomputable, and never
> overwrites it.

The complaint that produced it was precise: *"saddlebag cannot redo its own
work. Capture reads a transcript, asks a model to distil it, writes the
result, and keeps a path... The raw material is never in the database."*
Improve the prompt, change the pinned model, or find a bad extraction, and
there was nothing to run again.

Events fixed that for the tool layer. This design fixes it for the rest.

But one sentence from that design is the constraint everything here obeys:

> Events are the portable raw unit. A transcript exists in one harness of
> three. A per-session record would therefore be a Claude Code-only design
> wearing a general-purpose name.

That remains true, and this design does not touch it. Transcripts are an
**additive** surface. Nothing downstream may require one, extraction keeps
working for a session that has only `tool_call` events, and the tables are
simply empty for a harness that writes no transcript.

### The cost of delay is data, not effort

Of 35 recorded `saddlebag` session ids, two have no transcript file anywhere
on disk (a third, `smoke`, is a test fixture). That is the events design's
"rotation and deletion" worry confirmed by measurement rather than
hypothesised. Every session that ages out is permanently unrecoverable.

## What gets built

Four tables, five commands (`discover`, `designate`, `import`, `refresh`,
`status`), one spawn point, one pure module.

Two things are explicitly **out of scope** and get their own designs:
indexing and retrieval over transcripts, and labelling. A sketch of what
they will need is in "Keeping the training uses open" below, and it exists
only to check that the storage shape does not foreclose them.

## Scope: whose transcripts

Recording is enabled for exactly one project, `saddlebag`. A naive importer
mapping directory slugs to projects would therefore read 23MB and miss
184MB of the same project's history, because the tool was called `remem`
for most of it and part of the work happened in a worktree.

**Project ownership of a directory is provable, not guessable.** Transcript
filenames *are* session ids, and `events` already records which project each
session belongs to. Intersecting the two:

```
26  ~/.claude/projects/-Users-brandon-llmworkspace-remem      (112 transcripts)
 6  ~/.claude/projects/-Users-brandon-llmworkspace-saddlebag  ( 34 transcripts)
```

No name matching, no heuristic, no `--project` flag to get wrong.

Matching only proves *part* of a directory. `-remem` holds 112 transcripts
and only 26 carry recorded events, because recording was enabled partway
through. The other 86 are this project's history from before the pipeline
existed and are, by volume, most of the 156MB. They cannot be proven by
session id - only by the directory they sit in.

And matching cannot find a directory at all when *none* of its sessions were
recorded. `-remem--claude-worktrees-capture-impl` holds 9 transcripts and
28MB of this project's worktree history, and zero recorded sessions, so
`discover` proposes nothing for it. It is claimable only by a human who
knows it exists. That is the case which makes discovery a proposal rather
than an algorithm: evidence finds most of the corpus and can never find all
of it.

Hence the split between discovery and designation:

- **`bag transcripts discover`** intersects recorded session ids with
  filenames across `~/.claude/projects/*` and **proposes** claims with their
  evidence: *"-remem: 26 of 35 recorded saddlebag sessions, 112 transcripts
  total"*. It writes nothing.
- **`bag transcripts designate <dir>`** is the human act that claims one. It
  refuses a directory already claimed by a different project.

**Claiming a directory imports all of it**, including sessions that predate
recording. This needs saying plainly rather than being discovered later: the
per-project record gate governs *recording going forward*; a typed
designation is the deliberate, separate opt-in for *backfill*. Nothing
auto-claims, and `discover` never writes, precisely so that widening scope
is always a human act.

Where a session has recorded events, the import asserts the claiming project
and the recorded project agree, and reports a conflict if they do not. A
directory can hold sessions from more than one project if the working
directory moved, and guessing there would file traces under the wrong
project silently.

Paths are stored **absolute, as given** - deliberately unlike `reingest
designate`, which stores repo-relative and resolves against the git root.
These directories live outside any repository; there is no root to resolve
against.

## Data model

### `transcripts` (migration 020) - the source

```sql
create table transcripts (
  id           uuid primary key,
  owner_id     uuid not null references principals(id),
  project      text not null,
  harness      text not null,
  session_id   text not null,
  path         text not null,
  content      bytea not null,
  bytes        bigint not null,
  sha256       text not null,
  first_seen   timestamptz not null default clock_timestamp(),
  last_read    timestamptz not null default clock_timestamp(),
  unique (owner_id, harness, session_id)
);
```

`content` is `bytea`, not `text`. A transcript is a file another tool owns.
If one line is ever invalid UTF-8, `text` refuses the insert and the whole
session is lost rather than the line - and the entire point of the source
row is that it survives a parser that does not.

`unique (owner_id, harness, session_id)`, not on `path`. The directories
hold *different* sessions, so path is not identity, but the same session
must never land twice if two projects claim overlapping directories.
Session id is the real key and is already what `events` uses.

`harness` is a column, not a promise. Claude Code is the only harness with
a transcript today; the column exists so that a second one needs no
migration.

### `transcript_lines` (migration 021) - derived

```sql
create table transcript_lines (
  transcript_id uuid not null references transcripts(id) on delete cascade,
  seq           int  not null,
  type          text,
  uuid          text,
  occurred_at   timestamptz,
  raw           jsonb not null,
  primary key (transcript_id, seq)
);
create index on transcript_lines (transcript_id, type);
```

Explicitly derived, with `on delete cascade` and no independent lifetime.
Dropping and rebuilding this table must always be safe, so **nothing may
store anything only here**. Labels, chunks and training signals reference
`(transcript_id, seq)` from their own tables - a stable coordinate, because
JSONL line numbers do not shift under append.

`type` and `occurred_at` are nullable conveniences hoisted out of `raw` for
indexing. Claude Code's format is not ours and will change; a line whose
shape is unrecognised still stores, with nulls, rather than failing the
import. `raw` is always complete.

Observed line types in one real transcript, as a sense of the distribution:
`attachment` 377, `assistant` 344, `user` 188, then `last-prompt`, `mode`,
`permission-mode`, `atis-latch` at 54 each, `ai-title` 53, `system` 17,
`file-history-*` 15, `cost-state` 1. Most of the tail is harness
bookkeeping. None of it is filtered at capture; filtering is a read-time
concern.

### `transcript_paths` (migration 022) - claims

```sql
create table transcript_paths (
  owner_id   uuid not null references principals(id),
  project    text not null,
  path       text not null,
  added_at   timestamptz not null default clock_timestamp(),
  primary key (owner_id, project, path)
);
```

### `transcript_runs` (migration 023) - observability

```sql
create table transcript_runs (
  id             uuid primary key,
  owner_id       uuid not null references principals(id),
  project        text not null,
  trigger        text not null,
  started_at     timestamptz not null default clock_timestamp(),
  finished_at    timestamptz,
  files_seen     int not null default 0,
  files_new      int not null default 0,
  files_appended int not null default 0,
  files_rebuilt  int not null default 0,
  lines_written  bigint not null default 0,
  bytes_written  bigint not null default 0,
  anomalies      jsonb not null default '[]',
  failures       jsonb not null default '[]'
);
```

### No foreign key to `events`

Deliberate, and the same rule `entry_events.event_id` follows: pruning a
transcript must never be blocked by, or cascade into, an event row, and vice
versa. The two surfaces overlap in content and must not couple in lifetime.

## Import flow

### Two commands

`bag transcripts import` is typed and **fail-loud**: a person asked, so it
exits non-zero on failure and reports what it did.

`bag transcripts refresh` is the spawned half. It exits 0 on every path,
prints nothing to stdout, explains itself to stderr only behind
`BAG_HOOK_DEBUG`, and is the only caller that records `trigger='auto'`.
Same contract as `bag memory refresh` and `bag reingest run`, including the
`except BaseException` - which is there for a genuine `SystemExit` from a
library that calls `sys.exit()`, and for `KeyboardInterrupt`. It is *not*
there for `typer.Exit`, which subclasses `RuntimeError` and is caught by
`except Exception` too.

### The refresh is bounded; the backfill is not

First import of the two discoverable directories is 179MB across 146 files -
207MB across 155 if the worktree directory is claimed too - which must
never run inside a session-start hook. `refresh` handles only what is cheap
- new sessions, and files whose size changed - capped at a per-run file
count enforced **before** reading. The bulk backfill happens under `bag
transcripts import`, typed, once, with progress.

Designating a directory therefore does not trigger the backfill read at all.
It records a claim, and the CLI names the backfill command and what that
directory will cost.

### Incrementality, in the order the checks run

1. `stat` the file. Size equal to stored `bytes` -> **skip without reading**.
   This is the common case at every session start and costs one syscall per
   known transcript.
2. Size grew -> read the first `bytes` bytes, sha256 them, compare to the
   stored hash. A match proves append-only: read the tail, parse it, insert
   lines from the stored line count, append to `content`, update
   `bytes`/`sha256`/`last_read`.
3. Prefix hash mismatch -> the file was rewritten, not appended. Re-read
   whole, replace `content`, delete and rebuild the lines. Safe by
   construction, because lines are derived and hold nothing of their own.
4. Size shrank -> **refuse, and record an anomaly**. The stored copy is more
   complete than what is on disk, and the purpose of the source row is that
   a rotating file does not destroy the session. This is the one case where
   the file is not followed.

The append assumption - that Claude Code only ever appends - is the one
thing here taken on inference rather than measurement. Case 3 exists so that
being wrong about it is a rebuild rather than corruption, and the
implementation plan should include an explicit check of it.

### Trigger

`hookio.spawn_transcripts`, called from the same two places as its three
siblings - Claude Code's `SessionStart` and `bag hook context` - which is
the one trigger all three harnesses share. Current project only, like `bag
memory refresh`: sweeping every claimed project from any session start would
read directories the user is not looking at.

`CHILD_ENV_VAR` (`BAG_EXTRACT_CHILD`) gets a **fourth** check. The spawned
`claude -p` extractor must not trigger a refresh, or the extractor's own
child writes a transcript that the next refresh imports, without bound.
Rename it on every side in the same commit or not at all.

### Reading a live file is fine, and slightly useful

The current session's transcript is being appended to as it is read. The
import takes a consistent prefix and picks up the rest next session - which
means a session's trace lands even if it never reaches `SessionEnd`, unlike
the `session_end` event that carries `transcript_path` today.

### Parse failures never fail the import

A line whose shape is unrecognised still produces a row, with `type` and
`occurred_at` null. A line that is not valid JSON at all is recorded in the
run row as a per-line failure and skipped - and `content` still holds the
bytes, so a fixed parser recovers it later with a rebuild. That recovery is
the reason the source row exists.

## Observability

`transcript_runs` is written by the **service**, so typed, spawned and any
future caller record identically - the last of those being the caller with
no terminal, and the reason the table is built before it exists. One row per
project. A `--dry-run` writes none, because a row for it would make "last
run" describe a state that never existed.

Both commands open with `autocommit=True`, like `bag memory sync` and `bag
reingest run`: the started row must commit **before any file is read**, or a
crash rolls it back and "crashed" becomes indistinguishable from "never
ran". A Python exception is recorded as a failure with path `*` and
re-raised.

`bag transcripts status` renders the latest run in four distinct spellings -
never, clean, with failures, did not finish - and every spelling that has a
run **names its trigger**, for the reason `bag memory status` does: a
spawned refresh and a typed import leave identical rows, and believing the
automatic half ran when only a manual one had is the false premise the line
exists to prevent.

Then three things describing the state *now*, which a reader must not have
to infer from what last happened:

- **Claimed directories**, each checked on disk - present, or missing and
  named. A recorded path that no longer exists is reported as missing, never
  as clean.
- **Backlog**: files on disk in claimed directories, minus transcripts
  stored.
- **Irrecoverable sessions**: sessions with recorded events and no file
  anywhere. Today, 2. This number only grows, and it states the argument for
  importing sooner as a measurement rather than as urgency.

`--json` emits **one object, not a list** - like `bag memory status --json`,
deliberately unlike `bag reingest status --json` - because this command
resolves exactly one project by construction. The keys are the same in every
state, so an unclaimed project is a null `run` and an empty `paths` array,
not a shorter document.

`bag record status` gains one advisory line per unhealthy claimed project: a
run that did not finish, failures in the last run, a shrink anomaly, a
claimed directory never imported, or a missing directory. **Backlog alone is
not an advisory** - `refresh` is bounded by design, so a nonzero backlog is
the normal state between runs, and a line that fires every time is one
people learn to ignore.

Not in `bag doctor`, deliberately and for the same reason as the memory
designation and the block budget: doctor reads files and opens no database
so that it still works when the system does not, and every question here
needs a connection.

## Testing

One pure module, `transcript_file.py` - parsing and append classification,
no I/O and no store - so its tests carry no `db` marker and run on CI. Same
shape as `markdown.py`, `memory_file.py` and `session_size.py`.

The properties it must hold:

1. **`content` round-trips byte for byte.** Here this is the property
   itself, not a rejected proxy: if it does not hold, the source row is
   worthless.
2. **Parsing is total.** A file with N lines produces exactly N rows,
   including lines that are not valid JSON. A silently dropped line is the
   failure; a row with a null `type` is the correct outcome.
3. **Append classification is a pure function** of `(stored_bytes,
   stored_sha, new_size, new_prefix_sha)` returning `SKIP | APPEND |
   REBUILD | SHRUNK`. All four branches, no filesystem.
4. **Rebuild is idempotent.** Parsing the same `content` twice yields
   identical rows, which is the assumption everything downstream rests on.

Two contract tests, split the way the opencode hook contract is split, for
the same reason - a guard that skips on CI is the failure mode this project
has already been taught to distrust:

- **Always runs**: a small **synthetic** fixture transcript checked into the
  repo covering every line `type` seen in the wild, plus a malformed line
  and a non-UTF-8 byte.
- **May skip** (`@pytest.mark.transcript`): reads a real transcript from
  `~/.claude/projects` if one is present and asserts every `type` it finds
  is one the parser handles. Claude Code changes its format without telling
  us; expect this to fire eventually, as the opencode freshness test did
  mid-branch.

**No real transcript is ever committed.** The fixture is synthetic and
hand-written. A real one contains real code, real paths, and whatever the
session touched.

`db`-marked: import, refresh, discover, designate, status, and the run rows
- which is nearly the whole feature, so the skip-count warning applies with
full force.

Three existing rules this feature obeys rather than reinvents:

- **`hookio.spawn_transcripts` must be stubbed** in any test whose path
  reaches it. A real detached `bag` at `live_dsn` outlives the test and
  deadlocks conftest's truncate.
- **`CHILD_ENV_VAR` gets a fourth check**, renamed on every side or not at
  all.
- **The `except BaseException` needs a test that proves what it is for** - a
  genuine `SystemExit` and a `KeyboardInterrupt`. The unreachable-database
  case passes under either handler and proves nothing.

One failure mode with no precedent here: **volume**. A test must never load
156MB, and neither must the import - files are read and inserted one at a
time.

## Keeping the training uses open

Not a design. A check that the storage shape does not foreclose the four
stated uses, and a note of what each still needs.

A finding that shapes this section: **transcripts record every hook's full
stdout, exit code, duration and stderr**, including `SessionStart:startup`.
The `additionalContext` saddlebag injects sits verbatim in an `attachment`
line of every session's transcript.

Two consequences. Steering needs no new instrumentation and is answerable
*retroactively* across the whole corpus. And separately: the failure
CLAUDE.md calls invisible - `RulesExceedBudget` silently killing injection,
exit 0, no output - is **not** invisible. It is recorded, with the exit
code, and nothing has ever read it.

**Better retrieval ranking.** Needs (query, candidates returned, which was
used). Nothing records that today, but transcripts hold both halves: the
`bag search` call with its full output, and everything the agent did next.
Labels are recoverable from traces already held, which is the difference
between an instrumentation project and a query. A prospective `search_log`
would be cleaner and is a later decision, not a blocker.

**Better extraction.** The re-runnable input is what this design delivers.
What remains is provenance for transcript-derived entries - `entry_events`
links entries to events and there is no equivalent for lines. That is a
later table, and it inherits the existing rule: **no foreign key**, so
pruning a transcript leaves a visibly dangling provenance row rather than
blocking or cascading into `entries`.

**Steering what surfaces in the block.** Now the cheapest of the four. The
injected block is in the trace, the session that followed is in the trace,
and "was this rule referenced or followed" is a judgement a model can make
over a pair already held. The open question is what the judgement is, not
where the data lives.

**Fine-tuning.** 207MB is thin for anything beyond a LoRA on conventions and
style, and the real prerequisite is export plus redaction, which is separate
work. What this design owes it is only that nothing is lost at capture:
`content` is byte-exact, so any future export format is derivable without
re-reading files that may be gone.

**The one thing decided now:** labels and signals live in their **own
tables**, keyed on `(transcript_id, seq)`, never as columns on
`transcript_lines`. That table is derived and must stay droppable. The
moment a label lives there, rebuilding the parse destroys training data and
the "derived lives apart from its source" principle inverts.

## Redaction

Transcripts are stored **raw and unredacted**, consistent with every other
capture boundary here: filtering at recording caps what any future extractor
could ever see, and extraction is the layer meant to be fixable and re-run.

That reasoning covers capture only. Redaction belongs at **export or
publish**, which is where funes puts it - redacting at index time and
re-scanning each chunk before publishing. Neither export nor publish exists
yet, so neither does the redaction boundary. This is noted so that the
absence is a recorded decision rather than an oversight, and so that the
first export design knows it owns the problem.

The practical consequence today: a transcript carries more secret material
than a tool-call event does, and the database is local. Anything that makes
this corpus leave the machine must solve redaction first.

## What this design does not do

- No indexing, chunking or embedding of transcripts. Retrieval over them is
  a separate design, and this one deliberately lands first so that the data
  starts accumulating while that is argued.
- No change to extraction. It keeps reading events.
- No change to `search`, `kb`, `handoff`, or the context block.
- No second harness. The tables accommodate one; nothing produces one.
- No pruning policy beyond an explicit command, matching events: kept
  indefinitely, deleted only on request.
