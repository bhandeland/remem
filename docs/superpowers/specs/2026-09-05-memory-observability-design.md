# Making memory sync observable

Design, 2026-09-05.

## The problem

`remem memory sync` computes a great deal and keeps none of it. Adopted,
healed, edited, regenerated, deleted, renamed, unchanged, per-file failures,
and every conflict - all of it is printed once and lives only in a terminal
scrollback.

That is tolerable while a person is watching. Two things make it stop being
tolerable:

- **A conflict sidecar outlives the run that wrote it.** Nothing ever deletes
  a `<name>.remem-conflict.md`, deliberately - auto-deleting one risks
  destroying the copy the user needs. So an unresolved conflict is a
  permanent condition that is visible only if someone happens to run `remem
  memory status` from that project's directory. Six projects are designated
  on this machine.
- **The next caller has no terminal.** The backlog item after this one spawns
  sync from a hook, the way `hookio.spawn_ingest` spawns re-ingest. A
  detached process printing to a `/dev/null` stderr is exactly the shape that
  made automatic re-ingest unobservable, and `ingest_runs` (migration 017) is
  the fix that was built for it. Building the same layer first, rather than
  after a silent sync has been running for a week, is the whole point of
  doing this item before that one.

`--all` compounds both: it loops six projects and prints six reports, and the
one that failed scrolls past.

## What gets built

- Migration `018_memory_runs.sql`. One row per sync invocation, per project.
- `MemoryRun` in `domain.py`; `start_memory_run`, `finish_memory_run`,
  `latest_memory_run` on the store.
- `services/memory.sync` starts and finishes the row itself, so every caller
  - today's CLI, `--all`, and tomorrow's hook - records without knowing it.
- A history line in `remem memory status`.
- `memory.advisories()`, one line per unhealthy designated project, rendered
  by `remem record status`.
- `remem memory sync` moves to an autocommit session.

## The table

`memory_runs`, mirroring `ingest_runs`:

| column | type | meaning |
|---|---|---|
| `id` | uuid primary key | |
| `owner_id` | uuid not null references principals(id) | |
| `project` | text not null | one row per project, never one per `--all` invocation |
| `trigger` | text not null check in ('manual','auto') | `'auto'` is unused until sync is spawned from a hook |
| `started_at` | timestamptz not null default clock_timestamp() | |
| `finished_at` | timestamptz | null means the process died mid-run |
| `adopted` `healed` `edited` `regenerated` `deleted` `unchanged` | int not null default 0 | straight off `Report` |
| `renamed` | jsonb default '[]' | `[["old","new"], ...]` - named, not just counted, because a rename re-tags an entry |
| `conflicts` | jsonb default '[]' | names changed on both sides |
| `sidecars` | jsonb default '[]' | the subset that actually wrote a `.remem-conflict.md` |
| `failures` | jsonb default '[]' | `[{"name": ..., "reason": ...}]`; a Python exception is recorded at name `*` |

```sql
create index memory_runs_latest_idx
  on memory_runs (owner_id, project, started_at desc);
```

Every read is "the latest row for this project", which is the index's only
job. The lists are `jsonb` for the reason `ingest_runs` stores its lists that
way: their only readers are a status renderer and `--json`.

Rows are kept indefinitely. Nothing prunes them.

## Decisions

### The service writes the row, not the frontend

`services/ingest` already settled this: the run row is written by the service
for both the spawned refresh and the typed command. The reason is not
symmetry, it is that the caller who most needs the record is the one that
cannot be trusted to write it - a detached process nobody is watching.

Putting it in `sync()` means `remem memory sync`, `remem memory sync --all`,
and the hook-spawned run in the next backlog item all record identically, and
the third of those costs nothing to add.

### One row per project, never one per invocation

`--all` loops the designated projects and writes one row each, keyed the same
way `ingest_runs` is. A single row covering six projects could not say which
one failed, and a status screen that cannot name the failing project is the
failure `remem doctor` exists to refuse.

### A `--dry-run` writes no row

A dry run changes nothing on disk and nothing in the store. Recording it
would make "last run" describe a state that never existed, and the next
reader of `remem memory status` would be told the directory is in a condition
it has never been in. Dry-run output is for the terminal, which is where the
person who asked for it is standing.

### Sync moves to an autocommit session

The started row must be committed **before any file is read**, or it cannot
distinguish "crashed" from "never ran" - the one distinction that justifies
writing a started row at all. Inside `_session()`'s single transaction a
crash rolls the row back and the record is gone.

So `remem memory sync` opens with `autocommit=True`, as `remem reingest run`
and `remem events process` do. CLAUDE.md already names this as the pattern
for long-running work that records its own progress.

This also corrects an existing mismatch rather than introducing one. `sync`
writes files to disk as it goes. Under one transaction, a failure partway
through leaves those files written and rolls the store's side back - a disk
that has moved and a store that has not, which is the worse of the two
inconsistent states and the one the watermarks then have to reconcile. Under
autocommit, both sides stop at the same place.

The floor, stated: sync is not atomic either way, and was never atomic
against the filesystem. What autocommit buys is that the two halves stop
together, and that the run row survives to say so. Sync is idempotent and
re-runnable by design; re-running it is the documented recovery, and the two
gates - never delete a file whose checksum does not match its watermark,
never overwrite a file changed on both sides - hold on the next run exactly
as they held on this one.

### `trigger` is recorded now, though only one value can occur yet

The column carries `'manual'` today and `'auto'` when the hook-spawned sync
lands. It is here now because that caller is the reason this table exists,
and because adding a column later costs a migration for a case already known
to be coming. `ingest_runs` has the same column for the same reason, with a
check constraint rather than an enum: two values, and the check reads the
same.

### The advisory covers four conditions

`remem record status` is the fail-loud half of a fail-soft pipeline and
already carries the doctor and ingest advisories the same way. Memory sync
adds one line per unhealthy **designated** project:

| condition | why it is unhealthy |
|---|---|
| `.remem-conflict.md` sidecars on disk | nothing deletes them, so an unresolved conflict is permanent and otherwise invisible from anywhere but that directory |
| a run that started and never finished | the crashed-not-never-ran case the started row exists to state |
| per-file failures in the latest run | an unparseable or unwritable file, which today prints once and is gone |
| a designated project that has never synced | the designation exists and nothing has acted on it |

The last one is deliberately included despite being noisy for a project
synced rarely. It is a definite statement rather than an "I could not tell",
and it is the condition most likely to mean a designation was made and then
forgotten. It is worded as such, and it stops the moment a sync runs.

### The advisory may check other projects' directories, unlike ingest's

`remem reingest status` checks designated paths on disk **only** for the
project the current directory resolves to, because `ingest_settings` records
no working directory and any other project's paths cannot be resolved.

`memory_settings` records one (migration 015). That is what makes `remem
memory sync --all` expressible at all, and it makes the conflict check
honest for every designated project rather than a guess. So this advisory
checks the sidecars in each project's recorded directory.

Two consequences follow, and both are stated rather than papered over. A row
written before migration 015 reads back with no directory: it is **skipped by
name**, the same way `--all` skips it, rather than guessed at - re-designating
is the fix. And a recorded directory that no longer exists on disk is
reported as such, not treated as "no conflicts": an absent directory is a
different fact from a clean one.

### The history line follows `remem reingest status`'s four spellings

`remem memory status` gains one line for the latest run, in four distinct
spellings: never run, finished clean, finished with conflicts or failures,
and started but did not finish. The existing `Status` fields - `stale`,
`overlap`, `conflicts` - describe the directory **now**; the run line
describes what last happened to it. Both are wanted, and a reader must not
have to infer one from the other.

## Architecture

```
cli.py  memory sync / memory status / record status    formats only
services/memory.py  sync() starts and finishes the row
                    status() gains the latest run
                    advisories()                       every judgement
store.py  start_memory_run / finish_memory_run / latest_memory_run
backends/postgres/store.py                             the SQL
backends/postgres/migrations/018_memory_runs.sql
domain.py  MemoryRun  MemoryTrigger
```

`sync()` writes the started row before reading the directory, and the
finished row in a `finally`, so a Python exception is recorded as a failure
with the file `*` and re-raised - the shape `reingest run` uses.

`advisories()` iterates `designations()` - which carries each project's
recorded working directory - and calls `status()` once per project with that
directory. `memory.status()` answers for one project at a time, unlike
`ingest.status()`, which sweeps; the sweep therefore lives in `advisories()`
rather than being assumed to exist below it.

`advisories()` makes every judgement about what "unhealthy" means, so `remem
record status`, `remem memory status` and any future frontend cannot disagree
about it. This is the rule a previous session
learned the hard way: a service function feeding two surfaces must be
re-checked against both when changed for one.

Every advisory line ends with a pointer that names its scope, because a
pointer leading to a screen that contradicts it teaches the user the line
lies.

## Error handling

| failure | behaviour |
|---|---|
| sync raises partway | run row finished with the exception recorded as a failure at path `*`, then re-raised - sync stays fail-loud |
| process killed mid-sync | started row with `finished_at` null; reported as "did not finish" |
| `--dry-run` | no row written at all |
| project designated before migration 015 (no directory) | skipped by name in the advisory, as `--all` skips it |
| recorded directory missing on disk | reported as missing, never as clean |
| database unreachable | `_session` raises `typer.Exit(1)`, as everywhere else |

## Testing

No `db` marker, running on CI:

- the four history-line spellings are distinct strings, including the two
  that both describe a finished run.
- advisory wording for each of the four conditions, and that a healthy
  project produces no line.
- a designation with no recorded directory is named in the skip, not
  silently dropped.

`db`-marked:

- a started row is visible from a **second connection** before sync
  finishes. This is the test that actually proves the autocommit change; a
  same-connection read would pass under the old single-transaction code and
  prove nothing.
- `finish_memory_run` records every count off `Report`, and the jsonb lists
  round-trip.
- `--dry-run` writes no row, asserted by counting rows before and after.
- `--all` writes one row per project, asserted by project name.
- a sync that raises leaves a finished row carrying the failure, and the
  exception still propagates.
- a second principal's runs are never returned by `latest_memory_run`,
  seeded explicitly - a fresh fixture guarantees their absence, which is why
  they have to be put there.
- the advisory is empty for a healthy designated project and non-empty for
  each unhealthy condition, against real rows.

Every guard is watched failing before it is trusted, with `__pycache__`
cleared between variants: a mechanical `if X:` -> `if False and X:` patch
adds the same byte count for every guard, so two variants in the same second
can reuse each other's stale bytecode and report a false pass.

Any test whose path reaches `hookio.spawn_process` or `spawn_ingest` stubs
both.

## Out of scope

- Spawning sync from a hook. That is the next backlog item, and this is the
  layer it needs.
- Pruning `memory_runs`. Rows are kept indefinitely, as events and
  `ingest_runs` rows are.
- Deleting conflict sidecars, automatically or otherwise.
- Any change to the two sync gates, to `classify`, or to `_follow_renames`.
- Reporting run history in `remem doctor`, which opens no database.
