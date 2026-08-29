# Events, extraction and semantic recall - design

Date: 2026-08-28
Status: approved, ready for implementation planning
Builds on: docs/superpowers/specs/2026-08-26-capture-design.md
Supersedes the capture pipeline described there (see "Migration from capture").

## Purpose

Two failures, one design.

**remem cannot find what it knows.** Search is exact full-text with a trigram
fallback. Both match *words*. Asked for "config command" this repository's
store returned nothing, then four unrelated entries by trigram similarity, while
four memories written that morning about exactly that work sat in the table
unreturned. They were only found by listing rows in `psql` by date. A memory that
cannot be recalled is not stored, it is merely retained.

**remem cannot redo its own work.** Capture reads a transcript, asks a model to
distil it, writes the result, and keeps a *path* - `capture_jobs.transcript_path`.
The raw material is never in the database. Improve the prompt, change the pinned
model, or find a bad extraction, and there is nothing to run again: the
transcript is a file owned by another tool, subject to rotation and deletion.
Every session that ages out is permanently unrecoverable, so the cost of delay
here is data, not effort.

Both are instances of one principle, and this design adopts it as a rule:

> **Derived data lives apart from its source, is recomputable, and never
> overwrites it.**

Entries are derived from events. Vectors are derived from entries. Neither may
be the only copy of anything.

A third pressure decides the shape. remem should work under Claude Code, Cursor
and opencode, and those three do not agree on what they will give a plugin.
Recording per-tool-call events is the only raw material all three can supply.

## Terminology

Settled before the design, and binding on code, docs and CLI:

| Word | Means |
|---|---|
| **entry** | the unit of knowledge - unchanged |
| **note** | a kind of entry, alongside `doc` and `rule` (was kind `memory`) |
| **event** | one piece of raw material a harness handed us |
| **record** | what hooks do: write events (was "capture") |
| **extract** | events -> entries (was "distil") |
| **process** | run queued work (was "drain") |
| **prune** | delete events, on request - never on a schedule by default |
| **harness** | the agent tool remem is running under |
| origin **`extracted`** | an entry the extractor produced (was `capture`) |

"Memory" remains the informal word for any entry and is never a schema term.

## What the harnesses actually provide

Verified against the shipped integrations, not documentation: Claude Code's hook
payloads, Cursor's hook set as mapped in claude-mem's `cursor-hooks/PARITY.md`,
and opencode's `Hooks` interface in the installed
`@opencode-ai/plugin/dist/index.d.ts`.

| | integration shape | raw available | context injection |
|---|---|---|---|
| Claude Code | shell hooks | transcript file, tool events | stdout at `SessionStart` |
| Cursor | shell hooks | tool events only, **no transcript** | write `.cursor/rules/*.mdc` |
| opencode | in-process JS plugin | tool events, chat messages, **no transcript** | `experimental.chat.system.transform` |

Two conclusions follow, and they are the load-bearing decisions of this design.

**Events are the portable raw unit.** A transcript exists in one harness of
three. A per-session record would therefore be a Claude Code-only design wearing
a general-purpose name. One row per thing that happened is the only substrate
all three can fill.

**Neither Cursor nor opencode has a reliable session-end signal.** Extraction
cannot be triggered by an end hook, because two of three harnesses do not have
one. It is triggered by idleness instead - see "Extraction trigger".

## Requirements

1. Record events from any harness through a `remem` command, so hook wiring is
   adapter work and nothing else.
2. Keep raw events in the database, in full, so extraction can be re-run.
3. Extract entries from events without a session-end hook.
4. Keep events indefinitely by default, with prune available as an explicit
   command that leaves entries and provenance intact.
5. Recall entries by meaning, not only by word, without weakening the existing
   precision guarantees.
6. Make "recording nothing" visible. Fail-soft hooks must not be able to fail
   silently forever.
7. Every step runnable from cron with no daemon.

## Non-goals

- No worker service. claude-mem runs a local HTTP worker largely because a Node
  process per tool call is expensive; `remem whoami`, including connecting to
  Postgres, measures 130ms, which is affordable per event. If event volume ever
  makes it unaffordable, a daemon is a later, separable change - the command
  surface would not move.
- No cross-harness transcript normalisation. Claude Code's transcript path may
  ride along in a `session_end` payload, but nothing downstream may require it,
  and extraction must work for a session that has only `tool_call` events.
- No change to `handoff`, `kb`, or collections.
- No second embedding implementation. The seam is documented; one implementation
  ships.
- **No learned classification, and no PyTorch.** The tempting version is a
  classifier that decides which events are worth extracting, cheaper than asking
  a model. Not now, for two reasons: there are no labels except the extractor's
  own decisions, so the ceiling is imitating what we already have while
  inheriting its mistakes; and at this volume the extraction call is not the
  expensive part. The dependency matters too - `sentence-transformers` pulls
  PyTorch, gigabytes, onto the install path of a tool whose hooks are meant to
  be invisible. The local embedder is ONNX-based (`fastembed` shape): inference
  only, tens of megabytes, no torch.

  What this design does instead is keep the option open. Events are stored in
  full with `tool`, `harness` and timestamps, and `entry_events` means every
  event eventually carries an implicit label - *did anything durable come out of
  this?* - so a usable labelled corpus accumulates for free. Revisit when there
  is a year of it. Keeping events by default is what makes that possible: a
  scheduled prune would keep the corpus permanently shallow, and the whole point
  of accumulating raw is to have something to look back over.

  Separately, a large existing document corpus is the right way to test whether
  semantic recall works at all - a store of ten entries cannot answer it. That
  is an import path and an evaluation exercise, not training, and it belongs
  after the pipeline works. It would make the `entry_vectors` index question
  above urgent rather than incidental: at hundreds of thousands of vectors,
  pgvector needs a real HNSW index and the embedding run is hours, not minutes.
  Measure before choosing.

## Data model

Four pieces, each with a single job.

### `events` - raw, append-only

```sql
create table events (
  id            uuid primary key,
  owner_id      uuid not null references principals(id),
  project       text not null,
  harness       text not null,          -- 'claude-code' | 'cursor' | 'opencode'
  session_id    text not null,
  kind          event_kind not null,    -- 'tool_call' | 'message' | 'session_end'
  tool          text,                   -- null for non-tool events
  payload       jsonb not null,
  occurred_at   timestamptz not null,
  recorded_at   timestamptz not null default clock_timestamp()
);
```

`kind` is small and closed; everything harness-specific stays in `payload`,
unparsed. A harness changing its payload shape must not be able to break the
write path - parsing is the extractor's problem, and the extractor can be fixed
and re-run because the raw is still there.

`session_id` is the harness's own id, opaque to remem. It groups events for
extraction and survives into provenance.

Payloads are stored **in full**. Truncating at record time is lossy forever and
would degrade the extraction this table exists to enable. The mitigation is the
opt-in gate and an explicit prune the user reaches for, not partial capture and
not automatic expiry. See "Retention and exposure".

### `entries` - unchanged

Gains `origin='extracted'`; loses nothing.

### `entry_events` - provenance

```sql
create table entry_events (
  entry_id   uuid not null references entries(id) on delete cascade,
  event_id   uuid,                      -- deliberately no FK
  session_id text not null,
  harness    text not null,
  primary key (entry_id, event_id)
);

create index entry_events_event_idx on entry_events (event_id);
```

`session_id` and `harness` are denormalised on purpose. When prune deletes the
events, the row still records where the entry came from. `event_id` is allowed
to dangle - there is no foreign key, because a foreign key would force a choice
between blocking prune and erasing provenance, and both are worse than a
recorded pointer to something we have deliberately deleted.

**This row is an audit record, not a live relationship.** "This entry came from
event X, in session Y, on harness Z" is a fact about the past, and it stays true
after event X is deleted. A foreign key would model it as though it stopped
being true, which is the wrong claim. That framing is what makes the missing
constraint a decision rather than an omission, and it carries two rules:

- **No read path may dereference `event_id`.** The only query allowed to follow
  it is forensic - *show me the raw behind this entry, if we still have it* -
  and that query must treat absence as an ordinary answer, not an error. This is
  the same invariant as "entries must be self-contained", seen from the other
  side, and it is what keeps prune safe to run at all.
- **Dangling must be visible, never inferred.** `remem events prune` reports how
  many provenance rows it just left dangling, and the forensic lookup says
  "event pruned" rather than "not found". Silence about deleted raw is how a
  user concludes the provenance was never recorded.

Since prune is no longer scheduled (see "Retention and exposure"), dangling is
now the rare case rather than the expected steady state - but the design does
not depend on that, and must not start depending on it.

Ids are uuid7, so a dangling `event_id` cannot later be reused by a different
event and quietly acquire a wrong meaning.

The index on `event_id` exists because there is no foreign key to provide one:
without it, "what came out of this event" is a sequential scan, and that is the
query the labelled-corpus idea in the non-goals depends on.

### `entry_vectors` - derived, disposable

```sql
create table entry_vectors (
  entry_id   uuid not null references entries(id) on delete cascade,
  model      text not null,
  dim        int  not null,
  vector     vector not null,
  created_at timestamptz not null default clock_timestamp(),
  primary key (entry_id, model)
);
```

One implementation note this shape forces: pgvector will not index a `vector`
column of unspecified dimension. Since `model` determines `dim`, the index has
to be partial - one per model, `where model = '...'` with the column cast to a
fixed dimension - or the table has to be per-dimension. The planning step should
settle which; the choice does not change anything above it.

A separate table, not a column on `entries`. Changing embedding model is then
inserting rows and deleting old ones - never a migration, never a rewrite of the
source row - and two models can coexist while a re-embed runs. Losing the table
costs a re-run and no data. This is Chroma's relationship to claude-mem's SQLite,
expressed inside Postgres.

### `extract_jobs`

`capture_jobs` renamed and re-keyed from a transcript path to a session:
`(owner_id, project, harness, session_id)`, keeping `status`, `attempts`,
`error` and `entries_written` unchanged. The existing retry semantics -
`MAX_ATTEMPTS`, retry-by-id, failures recording both reason and the model's raw
output - carry over as they are.

## Flow

```
harness hook / plugin   ->  remem record event      one INSERT, fail-soft
cron (or SessionStart)  ->  remem events process    extract -> entries + provenance
cron                    ->  remem embed             entries lacking a current vector
on request              ->  remem events prune      extracted events, explicit window
```

Every step is a command. Nothing in the pipeline requires a specific harness,
and cron needs no daemon.

### Recording

`remem record event` reads a JSON payload on stdin, resolves project and owner,
and performs exactly one INSERT. It stays fail-soft in the established sense:
exit 0, print nothing, `REMEM_HOOK_DEBUG=1` explains to stderr.

The per-project opt-in gate moves across from capture unchanged and remains the
entire safety story. It is checked in the service, not the frontend.

### Extraction trigger

A session is extractable when it has unextracted events and no event newer than
`REMEM_IDLE_MINUTES` (default 20). `remem events process` finds those sessions,
creates or claims an `extract_job`, runs the extractor, writes entries with
`origin='extracted'`, and writes `entry_events` rows.

An idle rule rather than an end hook because two of three harnesses have no end
hook. A useful consequence: Claude Code needs no `SessionEnd` hook either, so
all three integrations reduce to "record events" plus "inject context", and
the harnesses become more alike rather than less.

`session_end` events are still recorded where a harness emits them. They are a
hint that shortens the idle wait, never a requirement.

### Extraction itself

The extractor's contract is unchanged from capture: the model is pinned
(`REMEM_EXTRACT_MODEL`, default `sonnet`), all output is untrusted and
shape-checked, capped by `MAX_ENTRIES`/`MAX_TITLE`/`MAX_BODY`, and filtered
before it reaches the store. `CHILD_ENV_VAR` still stops a spawned child's own
hooks from recursing.

What changes is the input: a list of event rows rather than a transcript file.

### Embedding

`remem embed` finds entries with no `entry_vectors` row for the configured
model and embeds them in batches. Idempotent; safe to re-run; safe to run
concurrently with a lock (see "Cron safety").

The embedder is a `Protocol` in the manner of `store.py`, with one local
in-process implementation shipped. No API key, no network on any path, no cost,
and every existing entry backfillable. Hosted implementations are a
configuration question for someone else's package.

### Prune

Nothing prunes on its own. `remem events prune --before 30d` deletes events that
are **both** older than the given window and already extracted. `--before` has
no default: the window is always something the user typed, so there is no
configured number quietly deleting history. Unextracted events are never deleted
by default - losing raw before it has produced anything is the one outcome the
whole design exists to prevent. `--force` overrides for a stuck session, loudly.

Prune reports what it deleted **and** how many `entry_events` rows it just left
dangling. That number is the cost of the run, and it is the only moment a user
can see it.

## Search

Three tiers, still never blended:

```
exact full-text  ->  semantic  ->  trigram
```

Semantic sits above trigram because meaning beats spelling; trigram remains last
as the typo net. Each tier runs only when the previous returned nothing, for the
reason the current fallback already documents: mixing approximate hits into a
result set that contains exact ones trades precision for a problem that does not
exist there.

`Hit.fuzzy: bool` becomes `Hit.match: exact | semantic | fuzzy`. One field
rather than an accumulating set of booleans, and the existing rule extends
unchanged: **every frontend must surface the marker**, because an agent handed
an unmarked approximate match cites it as certain.

`DEFAULT_ORIGINS` gains `extracted` in place of `capture`. The standing warning
applies - any origin missing from that list silently vanishes from search.

Entries with no vector are invisible to the semantic tier and reachable by the
other two. That is the correct degradation: an un-embedded entry is not lost,
only less findable, and `remem embed` fixes it.

## Retention and exposure

Full payloads mean `events` will contain file contents, command output, and
whatever a user pasted into a prompt.

**Events are kept indefinitely by default.** There is no retention window and no
`REMEM_EVENT_RETENTION_DAYS`. The purpose of this pipeline is to distil and
summarise over a long history so future decisions are better and past mistakes
are not repeated, and a store that expires its raw after two weeks cannot do
that. Depth is the feature; deleting it on a timer works against the thing being
built.

- Recording is opt-in per project, as capture was. That gate, not expiry, is the
  privacy story - it is what decides whether sensitive material is ever written.
- Prune is a first-class command, run deliberately: to reclaim space, or to drop
  a project or window the user does not want kept. It is not wired to cron and
  no install configures it to run.
- Local storage is the assumption. Events are on the user's own Postgres, not a
  service; growth is a disk question they can answer with prune when it matters.
- Entries must be self-contained. Nothing may lazily read an event back at read
  time, or pruning would silently break retrieval. This holds even though prune
  is now rare - the dangling `event_id` in `entry_events` is what makes the
  option survivable, and it only survives if nothing depends on the event.

## Failure visibility

Fail-soft hooks make "recording nothing, silently, forever" the default failure
mode. claude-mem shipped an opencode integration bound to event names opencode
never emitted; nothing was recorded for months and `install` reported success
throughout. remem has the same exposure and must answer it in two places:

1. **`remem record status`** reports, per harness: events in the last 24 hours,
   last event timestamp, sessions awaiting extraction, and failed jobs. Silence
   becomes visible on demand.
2. **Install verifies.** An adapter's install performs one live round-trip -
   record an event, read it back, delete it - before reporting success. An
   install that cannot demonstrate recording says so.

## Adapters

Unchanged seam: `agents/base.py` plus the `remem.agents` entry point group. Each
adapter owns its harness's wiring and nothing else.

| Adapter | records via | injects context via |
|---|---|---|
| claude-code | shell hooks calling `remem record event` | stdout at `SessionStart` |
| cursor | shell hooks calling `remem record event` | writes `.cursor/rules/remem.mdc` |
| opencode | JS plugin on `tool.execute.after` / `chat.message`, shelling out via `$` | `experimental.chat.system.transform` |

Context injection differing per harness is why it is an adapter capability,
probed with `getattr` and documented on the Protocol, exactly as
`env_settings()`/`settings_path()` established in the config work. A capability
that raises degrades to a warning; a broken third-party adapter must never be
why remem will not run.

Only the claude-code adapter ships in this change. Cursor and opencode are
proven by the design and left to follow.

## Cron safety

Every command in the pipeline is expected to run unattended and possibly
overlapping:

- Idempotent. Running twice does the work once.
- Guarded by a Postgres advisory lock per command and owner, so a slow run and
  its successor do not both process the same session.
- Quiet on success, non-zero and explanatory on failure - the inverse of the
  hook contract, because cron output nobody reads is worse than silence.

## Errors

| Condition | Behaviour |
|---|---|
| Recording disabled for the project | `record event` exits 0, does nothing, explains under `REMEM_HOOK_DEBUG` |
| Malformed event payload | rejected loudly by `record event`; the hook still exits 0 |
| Extractor returns unusable output | job fails, records reason and raw output, retries to `MAX_ATTEMPTS` |
| No embedder available | `embed` exits non-zero; search degrades to two tiers |
| Prune would delete unextracted events | refuses without `--force` |
| Prune run with no `--before` | refuses; there is no default retention window |
| Adapter capability raises | warn, degrade, continue |

## Testing

- Contract test per adapter asserting we subscribe only to events the harness
  actually emits - for opencode, checked against the installed
  `@opencode-ai/plugin` types. This is the regression guard against the silent
  dead loop described above.
- Round-trip: record events from fixtures for each harness shape, process,
  assert entries and `entry_events` rows.
- Prune leaves entries and provenance intact, refuses unextracted events, and
  refuses to run at all without an explicit `--before` - nothing deletes events
  on a default.
- Search tier ordering, including that a semantic hit is marked `semantic` and
  that an exact result never contains one.
- Re-embedding under a changed model leaves the old rows until deleted and never
  touches `entries`.
- Idle trigger: a session with recent events is not extracted; the same session
  after the idle window is.
- The packaging test extends to any new migrations and adapter assets.
- Migration: a collection whose `query` filters on kind `memory`, created before
  `007`, resolves to the same entries after it. This is the jsonb rewrite in
  migration step 4 and the one failure mode that would otherwise be silent.
- Migration: an entry with `origin='capture'` is readable as `extracted`, and a
  pending `capture_jobs` row survives in `capture_jobs_legacy` rather than being
  translated or dropped.
- Migration: a project opted in before `006` is still opted in after it, and
  `record event` writes for it. The `capture_settings` rename touches the gate
  that decides whether anything is recorded at all, and a missed call site fails
  soft and silently.
- Forensic lookup of a pruned event reports "event pruned", not "not found", and
  no ordinary read path joins `entry_events` to `events`.

Database-backed tests skip when Postgres is unreachable, so a green run means
nothing unless the skip count is zero.

## Migration from capture

One migration, `007_events.sql` - `006` is the semantic-recall migration,
which shipped first. It is the only part of this design that touches
existing data, so each step is spelled out with what it can and cannot break.

**1. New tables.** `event_kind`, `events`, `entry_events`, `entry_vectors`. Pure
creation; nothing existing is read or altered.

**2. `extract_jobs` is a new table, not a re-keyed `capture_jobs`.** The earlier
draft renamed and re-keyed in place. That is the wrong trade. The old key is a
`transcript_path not null`; the new one is `(owner_id, project, harness,
session_id)`, and the two do not convert: a pending capture job names a
transcript, while the new extractor's input is *events*, which do not exist for
that session and never will. Any translation would be inventing rows.

So: create `extract_jobs` fresh, and rename `capture_jobs` to
`capture_jobs_legacy` - untouched, unread, no code path referring to it, dropped
in a later migration once the user has confirmed they want nothing from it.
Pending old jobs are not carried forward and not silently deleted; they sit
there, inspectable, and `remem events status` mentions them once if any are
`pending` so the dead spool is visible rather than mysterious. `capture_status`
is likewise left in place for the legacy table and a new `job_status` created
alongside it, so no enum is mutated while a table still uses it.

This costs one dead table for a release and removes every way the step can
corrupt data. Renaming beats transforming.

**3. Enum renames.** `alter type entry_origin rename value 'capture' to
'extracted'` and `alter type entry_kind rename value 'memory' to 'note'`.

Both are safe inside the caller's transaction - unlike `ALTER TYPE ... ADD
VALUE`, which is the famous footgun, `RENAME VALUE` is transactional and rewrites
no rows: the label changes, the ordinal does not. This matters because
`migrate()` runs inside the caller's transaction and must be able to roll back.

Existing `origin='capture'` entries become `extracted` by the rename. They have
no events and therefore no provenance - correct, and visible.

**4. The gap the rename does not close: `collections.query` is `jsonb`.** A smart
collection filtering on kinds stores the literal string `"memory"` in JSON, and
`ALTER TYPE ... RENAME VALUE` does not reach inside jsonb. Without a data update,
every existing collection that filters on `memory` silently matches nothing after
the migration - and an empty query matching nothing, forever, is precisely this
codebase's documented sharp edge. The migration must therefore also rewrite
`"memory"` to `"note"` inside `collections.query`, and a test must assert a
pre-migration collection still resolves to the same entries afterwards. This is
the single highest-risk line in the migration; it is also the one a reader would
never think to look for, which is why it gets its own step.

**5. `capture_settings` becomes `record_settings` - a live table, renamed in
one commit.** Unlike `capture_jobs`, this table is not dead: it holds the
per-project opt-in, which the retention decision above promoted into the entire
privacy story, and it is read on the path of every recorded event. Its shape is
unchanged - `(owner_id, project, enabled)` - so this is a bare `alter table
rename`, with no data transformation and nothing to get wrong in SQL.

The risk is not the rename, it is a partial one. Three statements in
`backends/postgres/store.py` name the table in string SQL, where no type checker
will catch a miss, and the enabled-check is the gate that decides whether
recording happens at all. A missed call site does not error usefully; it fails
the opt-in lookup, and a fail-soft hook then records nothing, silently - the
exact failure this design was written to prevent. So: rename and update all
three call sites in the same commit, and let the round-trip test in "Failure
visibility" be what proves the gate still answers.

**6. Names outside the database.** `REMEM_CAPTURE_MODEL` becomes
`REMEM_EXTRACT_MODEL`; the old name is read as a fallback for one release and
warns. `remem capture *` becomes `remem record *` and `remem events *`, with the
old spellings kept as hidden aliases that warn. `CHILD_ENV_VAR`
(`REMEM_CAPTURE_CHILD`) is checked by both hooks and by the spawned child - it is
renamed on every side in the same commit or not at all, per the standing warning
in CLAUDE.md.

**7. Forward-only, and loud about it.** remem is installed per-user with
`uv tool install`, so binary and database move together - there is no rolling
deploy to stage this for. The real exposure is the opposite direction: an *older*
`remem` from another checkout or worktree pointed at a migrated database. It will
fail on the renamed enum labels, and that failure must stay loud. No compatibility
shim reads both spellings; a version that cannot understand the schema should say
so rather than half-work.

## Open questions

None blocking. Two worth revisiting after use:

- Whether `kind` needs a fourth value for harness-specific lifecycle events.
  Deliberately deferred: adding an enum value later is cheap, and guessing now
  invites a value nothing ever writes.
- Whether extraction should see events from more than one session at a time.
  Cross-session extraction might find patterns a single session cannot, at a
  cost in prompt size and attribution clarity. Not in this design.
