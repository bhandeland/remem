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
| **prune** | delete events past retention |
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
4. Prune events on a retention policy, leaving entries and provenance intact.
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
would degrade the extraction this table exists to enable; the mitigations are
short retention and prune, not partial capture. See "Retention and exposure".

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
```

`session_id` and `harness` are denormalised on purpose. When prune deletes the
events, the row still records where the entry came from. `event_id` is allowed
to dangle - there is no foreign key, because a foreign key would force a choice
between blocking prune and erasing provenance, and both are worse than a
recorded pointer to something we have deliberately deleted.

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
cron                    ->  remem events prune      extracted events past retention
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

`remem events prune --before 30d` deletes events that are **both** older than
the window and already extracted. Unextracted events are never deleted by
default - losing raw before it has produced anything is the one outcome the
whole design exists to prevent. `--force` overrides for a stuck session, loudly.

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
whatever a user pasted into a prompt - including material that should not sit in
a database indefinitely.

- Default retention is short: **14 days**, `REMEM_EVENT_RETENTION_DAYS`.
- Recording is opt-in per project, as capture was.
- Prune is a first-class command, expected to run from the same cron as process.
- Entries must be self-contained. Nothing may lazily read an event back at read
  time, or pruning would silently break retrieval.

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
| Adapter capability raises | warn, degrade, continue |

## Testing

- Contract test per adapter asserting we subscribe only to events the harness
  actually emits - for opencode, checked against the installed
  `@opencode-ai/plugin` types. This is the regression guard against the silent
  dead loop described above.
- Round-trip: record events from fixtures for each harness shape, process,
  assert entries and `entry_events` rows.
- Prune leaves entries and provenance intact and refuses unextracted events.
- Search tier ordering, including that a semantic hit is marked `semantic` and
  that an exact result never contains one.
- Re-embedding under a changed model leaves the old rows until deleted and never
  touches `entries`.
- Idle trigger: a session with recent events is not extracted; the same session
  after the idle window is.
- The packaging test extends to any new migrations and adapter assets.

Database-backed tests skip when Postgres is unreachable, so a green run means
nothing unless the skip count is zero.

## Migration from capture

1. New migration: `events`, `entry_events`, `entry_vectors`, `event_kind`;
   rename `capture_jobs` to `extract_jobs` and re-key it to a session;
   `alter type entry_origin rename value 'capture' to 'extracted'`;
   `alter type entry_kind rename value 'memory' to 'note'`.
2. Existing `origin='capture'` entries become `extracted` by the rename. They
   have no events and therefore no provenance - correct, and visible.
3. `REMEM_CAPTURE_MODEL` becomes `REMEM_EXTRACT_MODEL`; the old name is read as
   a fallback for one release and warns.
4. `remem capture *` commands become `remem record *` and `remem events *`, with
   the old spellings kept as hidden aliases that warn.

## Open questions

None blocking. Two worth revisiting after use:

- Whether `kind` needs a fourth value for harness-specific lifecycle events.
  Deliberately deferred: adding an enum value later is cheap, and guessing now
  invites a value nothing ever writes.
- Whether extraction should see events from more than one session at a time.
  Cross-session extraction might find patterns a single session cannot, at a
  cost in prompt size and attribution clarity. Not in this design.
