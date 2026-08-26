# remem automatic session capture - design

Date: 2026-08-26
Status: approved, ready for implementation planning
Builds on: docs/superpowers/specs/2026-08-26-remem-design.md

## Purpose

remem v1 stores only what someone deliberately writes. Capture adds a producer:
when a Claude Code session ends, its transcript is distilled into a small number
of durable entries with `origin='capture'`, without anyone having to remember to
record them.

The v1 design anticipated this. Its deferred table reads "Automatic session
capture -> a producer calling `services/write.py` with `origin = 'capture'`",
and the `origin`, `agent`, and `session_id` columns already exist for it.
Nothing here changes the storage model.

## Requirements

1. Capture happens automatically at session end, with no per-session user action.
2. It never delays or breaks a session. The hook does bounded work and exits 0.
3. Captured entries are searchable but do not auto-inject into context blocks.
4. Capture is off until enabled for a project.
5. Failures are visible and diagnosable, not silent.
6. The test suite never spawns an LLM.

## Non-goals

- Streaming per-tool-use capture. One distillation per session, not per turn.
- A task framework (Celery, arq, dramatiq) or a message broker. See below.
- Regex redaction of transcripts. The safety gates are opt-in and the prompt.
- A review queue. Entries land directly once a project is opted in.
- Capture for agents other than Claude Code. The `Distiller` seam allows it.

## Architecture

```
SessionEnd hook          capture_jobs (Postgres)        drain
-----------------        -----------------------        -----
read payload        ->   INSERT one pending row    ->   claim (SKIP LOCKED)
exit 0 always                                           distill via claude -p
                                                        validate + dedup
                                                        write entries
                                                        mark done | failed
```

The hook does one INSERT. Everything fragile - subprocess, LLM, parsing -
happens in the drain, where it can be retried and inspected.

The drain is triggered by a detached spawn from the next SessionStart hook, or
run on demand with `remem capture drain`.

Claiming a job is one statement: `select ... where status = 'pending' ... for
update skip locked limit N`, followed by setting those rows to `running` and
incrementing `attempts` in the same transaction. A job left in `running` by a
killed drain is reclaimed once it is older than a stale threshold (10 minutes),
so a crash cannot strand work in a state nothing retries.

### Why not Celery

Evaluated and rejected. `celery` 5.6.3 does install and import on Python 3.14
(verified) but declares support only through 3.13.

The deciding argument is not the version: `capture_jobs` already IS a durable
queue. Adding Celery means either its broker replaces that table - losing the
audit trail and `capture status` that motivated the spool - or both exist and
must be reconciled. Celery would also add a broker container and a worker
process that must be running for capture to happen at all.

What it would buy is already covered: retries by the `attempts` column,
concurrency by `SELECT ... FOR UPDATE SKIP LOCKED`, monitoring by
`capture status`. Distributed workers are irrelevant to a single-user tool
distilling a handful of sessions a day.

Revisit if capture grows into several kinds of background work (distill, embed,
re-index, summarise) or spreads across machines. At that point `arq` or
`dramatiq` are the lighter first look, since neither needs the full
broker-plus-beat apparatus.

## Data model

Migration `004_capture.sql`.

```sql
create type capture_status as enum ('pending','running','done','failed');

create table capture_settings (
  owner_id uuid not null references principals(id),
  project  text not null,
  enabled  boolean not null default true,
  created_at timestamptz not null default clock_timestamp(),
  primary key (owner_id, project)
);

create table capture_jobs (
  id uuid primary key,
  owner_id uuid not null references principals(id),
  project text not null,
  session_id text,
  transcript_path text not null,
  status capture_status not null default 'pending',
  attempts int not null default 0,
  error text,
  entries_written int not null default 0,
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp()
);

create index capture_jobs_pending_idx on capture_jobs (owner_id, created_at)
  where status = 'pending';
```

The job records the transcript's PATH, not its contents. Transcripts reach
megabytes and already exist on disk under `~/.claude/projects/`. If the file is
gone by drain time the job fails with that reason rather than silently
succeeding with nothing.

The pending index is partial, matching the pattern established in migration 002.

## The recursion guard

The distiller spawns `claude -p`, which is itself a Claude Code session, which
fires its own SessionEnd hook, which would enqueue another job, which would
spawn another `claude -p`. This compounds without bound.

The drain sets `REMEM_CAPTURE_CHILD=1` in the subprocess environment. The
SessionEnd hook returns immediately when it sees that variable. This is a
correctness requirement, not a nicety, and it gets its own test.

## Distillation

```python
@dataclass(slots=True)
class CapturedEntry:
    title: str
    body: str
    kind: Kind
    tags: list[str]

class Distiller(Protocol):
    def distill(self, transcript: str, project: str) -> list[CapturedEntry]: ...
```

`ClaudeCliDistiller` shells out to `claude -p` with a bounded timeout and
`REMEM_CAPTURE_CHILD=1` set. It is chosen over the Anthropic API because it
reuses the user's existing Claude Code authentication: no second API key, no
second bill, nothing to configure before capture works.

The prompt instructs the model to return at most 5 durable memories as a strict
JSON array of `{title, body, kind, tags}`; to record nothing containing
credentials, tokens, environment values, file contents, or transient task
detail; and to return `[]` when nothing is worth keeping. It restates the
"when NOT to write a memory" guidance from the bundled skill so captured entries
meet the same bar as deliberate ones. That prompt text is the main thing between
this feature and a store full of noise: it is functional code, not documentation.

### Validation

The validator does not trust the model.

- Unparseable output fails the job, recording the first 500 characters of raw
  output in `error`. It never raises.
- Entries missing fields, or carrying a `kind` outside memory/doc/rule, are
  dropped individually rather than failing the batch.
- Title and body are length-capped (200 and 4000 characters).
- More than 5 entries are truncated to 5.
- **An empty array is a success**, recorded as `done` with `entries_written = 0`.
  Most sessions contain nothing durable. Treating that as failure would leave
  `capture status` permanently red and train the user to ignore it.

### Deduplication

Before writing, an entry is skipped when one with the same title already exists
for that project with `origin='capture'` and no `superseded_by`.

Capture runs after every session and re-derives the same facts repeatedly; over
a month "we use pgbouncer" would land twenty times. Unbounded accumulation of
near-identical entries is the characteristic failure of capture systems, and
title matching prevents the bulk of it for one indexed lookup.

## Keeping captures out of context blocks

`Query` gains an `origins` include-filter (empty means all origins).
`kb.resolve` passes `[Origin.HUMAN, Origin.AGENT]` when resolving a collection's
query branch.

Captured entries therefore appear in `search` and `recall`, where someone asked
a question, but never inject themselves into the block that loads at every
session start. Pinned members bypass the query branch entirely, so
`remem kb pin` promotes a good captured entry deliberately.

This falls out of one filter rather than a special case, and keeps
machine-written text out of the one surface where it would crowd out
hand-written rules.

`origins` MUST be applied in `fuzzy_search` as well as `search`. The
typo-tolerant fallback was added after the v1 spec, and a filter honoured by
only one of the two paths would appear to work until a query happened to miss
exactly, at which point captured entries would reappear.

## Interfaces

### CLI

```
remem capture enable  [--project X]     # defaults to the cwd's basename
remem capture disable [--project X]
remem capture status  [--json]
remem capture drain   [--limit N] [--job ID]
remem hook session-end
```

`capture status` reports pending and failed counts and recent errors, and is the
answer to "why has nothing been captured".

### Installer

`remem install claude-code` registers a SessionEnd hook beside the existing
SessionStart one, idempotently, and states in its output that capture is off
until enabled per project. An automatic feature nobody knows how to turn on is
the same discoverability failure as the knowledge-base slug convention.

## Failure handling

- The SessionEnd hook holds to the SessionStart hook's standard: bounded work,
  nothing on stdout, exit 0 unconditionally, `REMEM_HOOK_DEBUG` explaining an
  empty result on stderr.
- The drain wraps each job. A failure records its reason and moves to the next
  job; the drain never raises.
- `attempts` caps automatic retries at 3. Beyond that the job stays `failed` and
  visible, retryable only via `--job ID`. A permanently failing job should be
  something the user can see, not something that spins.
- Missing transcript, `claude` absent from PATH, and timeout are distinct,
  readable errors rather than one generic failure.

## Testing

- A `FakeDistiller` covers validation, dedup, empty-as-success, malformed JSON,
  and oversized fields. **No test spawns `claude`**: the suite stays offline,
  fast, and free.
- Hook tests cover enqueue-when-enabled, silence-when-disabled, the
  `REMEM_CAPTURE_CHILD` recursion guard, and exit 0 on every failure path.
- A concurrency test runs two drains against one pending job and asserts
  `SKIP LOCKED` prevents double-processing.
- A knowledge-base test asserts a captured entry is absent from a rendered
  context block via the query branch, and present once pinned.
- `ClaudeCliDistiller`'s subprocess construction is tested by asserting the
  command and environment it would run, without executing it.

## Files

| File | Responsibility |
|---|---|
| `src/remem/distill/base.py` | `Distiller` protocol, `CapturedEntry`, validation |
| `src/remem/distill/claude_cli.py` | the `claude -p` implementation |
| `src/remem/services/capture.py` | enqueue, claim, drain, dedup, settings |
| `src/remem/backends/postgres/migrations/004_capture.sql` | schema |
| `src/remem/agents/claude_code/hook.py` | `session_end()` |
| `src/remem/cli.py` | `capture` sub-app, `hook session-end` |
| `src/remem/agents/claude_code/adapter.py` | SessionEnd registration |
| `src/remem/store.py`, `backends/postgres/store.py` | job and settings persistence |

## Deferred from this design

| Deferred | How it would arrive |
|---|---|
| Capture for other agents | a second `Distiller` plus that agent's hook |
| Semantic dedup | compare embeddings once the `Embedder` seam is filled |
| Scheduled unattended drain | a launchd agent or systemd timer calling `capture drain` |
| Review queue | a `pending` entry state plus `capture review` |
