# Making automatic re-ingest observable

Design, 2026-09-04.

## The problem

Automatic re-ingest is fail-soft in the strongest form this repo has: `remem
reingest run` exits 0 on every path, prints nothing, and explains itself only
to stderr behind `REMEM_HOOK_DEBUG`, on a detached process whose stderr is
`/dev/null`. That is the right contract for something a session start spawns.
It also means that after the run, nothing on the machine can say whether it
happened.

Four symptoms were logged on 2026-09-04, and they have one cause - fail-soft
automation with no after-the-fact record:

- **(a)** Nothing records that an automatic re-ingest ever ran. `refresh`
  computes a full `RefreshResult` - counts, per-path failures, the embed error
  - and hands it to `hookio.debug`, which discards it. There is no job row and
  no timestamp. `remem doctor` cannot help: it opens no database by design.
- **(b)** Designated paths are validated when designated and never again. A
  directory renamed after `remem reingest designate` is a `FileNotFoundError`
  on every run, which `ingest_paths` catches into `report.failures`, which
  (a) then discards.
- **(c)** A healthy first ingest and a duplicating one print the same report.
  `12 new, 0 changed, 0 unchanged, 0 swept` is what a new document looks like
  and also what a document ingested under a second identity looks like.
- **(d)** `remem ingest` identifies a chunk by the path as typed, so running
  it from a subdirectory - or with an absolute path - stores a different
  `src:` tag from the one a root-relative run or the automatic refresh
  stores, and duplicates every chunk instead of superseding it. A stored rule
  says to run from the root; a rule is not a fix.

This is one piece of work, not four patches. (a) and (b) are the same fix -
persist what `refresh` already knows. (d) removes the ordinary cause of (c),
and what (c) needs beyond that is a warning at the one moment the
duplication is visible: when a file comes in entirely new.

## What gets built

- A `ingest_runs` table (migration 017) and a row per run, written by the
  service, opened before any file I/O and finalised in a `finally`.
- `remem reingest status` shows the latest run per project, with a
  `--json` form, and checks designated paths against disk for the project
  the current directory resolves to.
- `remem record status` carries one advisory line per designated project
  whose latest run failed, never finished, or has a designated path missing
  on disk.
- `remem ingest`, inside a repository, identifies chunks by their path
  relative to the repository root, the same identity the refresh uses.
- A per-file twin warning when a file comes in entirely new and a live
  anchor with the same filename already exists under another `src:` path.

## Decisions

### One row per invocation, kept, written by the service

`ingest_runs` follows `extract_jobs`: a per-owner, per-project row holding
what the run did.

| column | type | meaning |
|---|---|---|
| `id` | uuid | primary key |
| `owner_id` | uuid | references `principals` |
| `project` | text | |
| `trigger` | text | `auto` (spawned `reingest run`) or `manual` (`remem ingest`) |
| `archive` | boolean | which origin a manual run wrote; `false` for auto runs, which cover both halves |
| `started_at` | timestamptz | `clock_timestamp()` default |
| `finished_at` | timestamptz | null until the run finalises |
| `created`, `changed`, `unchanged`, `swept`, `embedded` | int | the counts `Report` and `RefreshResult` already hold, default 0 |
| `failures` | jsonb | list of `{"path": ..., "reason": ...}`, default `[]` |
| `twins` | jsonb | list of `{"path": ..., "existing": ..., "live": n}`, default `[]` |
| `embed_error` | text | null when the embed step ran or had nothing to do |

One index, `(owner_id, project, started_at desc)`, because every read is
"the latest row for this project".

**One row per invocation, not per designation half.** The refresh loops both
halves in one process and the failures carry their paths, so per-half rows
would only make "did the last run succeed" a two-row question.

**The service writes the row, not the CLI.** `refresh` calls
`store.start_ingest_run` before reading any file and `store.finish_ingest_run`
in a `finally`. A run that dies between the two - a crash, a killed process -
leaves a row with `started_at` set and `finished_at` null. That is what makes
"crashed" distinguishable from "never ran", which is the exact gap in (a). If
the database goes away mid-run, the finalising update fails too; that
exception reaches the `BaseException` guard `reingest run` already has, and
the row stays unfinished, which is the truth.

**Manual `remem ingest` records a row as well**, with `trigger='manual'`, so
"when was this project last ingested at all" has one answer regardless of
who typed it. It goes through the same two store calls. Manual ingest is
fail-loud, and stays so: a failure to write the run row is an error like any
other there.

**Rows are kept indefinitely.** No prune command. Events set the precedent -
nothing prunes on a schedule - and a run row is a few hundred bytes.

**What this cannot record.** Writing the row needs the database, so a session
start while Postgres is down still leaves nothing. `reingest status` reports
the age of the newest row rather than inferring anything from its absence.

### `remem reingest status` becomes a status, not a listing

Today it prints the designated paths. It keeps that and adds, per project,
the latest run:

```
remem (default): docs/superpowers/specs, docs/superpowers/notes
remem (archive): docs/superpowers/plans
last run: auto, 2026-09-04 14:02, 3 new, 1 changed, 40 unchanged, 0 swept, 4 embedded
  failed: docs/superpowers/notes: [Errno 2] No such file or directory
  twin: docs/a.md is new, but src:notes/docs/a.md has 12 live chunks
  embed skipped: fastembed is not installed
missing on disk: docs/superpowers/notes  (checked against /Users/brandon/llmworkspace/remem)
```

The last-run line has four states and each is spelled differently, because
they call for different actions:

| state | rendering |
|---|---|
| no row | `last run: never. A session start inside this repository spawns one.` |
| finished, no failures | the counts line |
| finished, with failures | the counts line, then one `failed:` line per failure |
| started, `finished_at` null | `last run: auto, started 2026-09-04 14:02, did not finish` |

**The on-disk check runs only for the current project.** Designations belong
to a project, and `ingest_settings` deliberately stores no working
directory - the root is derived at the point of use. So `reingest status`
can check paths against `repo_root()` only when the project being shown is
the one the current directory resolves to. Shown from anywhere else, with
`--project X`, the line reads `paths not checked: run from inside X's
repository`. A negative verdict names the ground it covered; "not missing"
about a directory never examined is the confident lie the diagnostics rule
exists to prevent.

`--json` is added, because the handoff's own gotcha - auditing summaries by
parsing the rendered block - is the shape this should not repeat. The JSON
carries the designation, the latest run row as stored, and the on-disk check
as `{"checked_against": path | null, "missing": [...]}`.

### The advisory lives in `remem record status`

`record status` is the fail-loud half of a fail-soft pipeline, and it
already carries the doctor advisory the same way. A new
`ingest_service.advisories(store, owner_id, root)` returns one line per
designated project whose latest run finished with failures, or started and
never finished, or - for the project `root` resolves to, and only that one -
has a designated path missing on disk. Each line ends with the pointer
`see: remem reingest status --project X`, the way the doctor advisory carries
its scope, because a pointer that leads to a contradictory screen teaches the
user the line lies.

The lines are passed into `events.status` as a second list beside
`hook_advisories`, rendered after them, and appear in `--json` under
`ingest_advisories`. Computing them is wrapped in `cli.py` exactly as the
doctor call is: a failure to compute the advisory must not take down the
status command it decorates. It does not change the exit code.

Not in the SessionStart context block - it would spend rule budget on
operational noise every session until fixed, and the block has under 2,000
chars of headroom. Not in `remem doctor`, which opens no database.

### `remem ingest` identifies by the repository-relative path

Inside a repository, the CLI translates each argument before calling the
service: resolve it, make it relative to the working tree's top level, and
pass `root=` that top level. `ingest_file` already reads at `root / path`
and identifies by `path`, and `_discover` already keeps its results in that
frame - the whole change is in what the CLI hands over.

The top level is the **working tree's**, from `git rev-parse
--show-toplevel`, not `repo_root()`, which resolves a worktree to the main
checkout. A file in a worktree is not under the main checkout's root, so the
relative path could not be computed against it. The relative path is the
same either way, which is what makes a worktree ingest and a main-checkout
ingest of the same file the same identity - the same property `resolve_project`
gives the project name. A new `project.toplevel(start) -> Path | None`
provides this; `None` outside a repository.

Running from the root with relative paths - what the stored rule says to do
and what every ingest on this machine has done - produces byte-identical
`src:` tags. Nothing is re-identified for those users. Subdirectory runs and
absolute paths inside the repository now produce the tag the refresh would.

**An argument outside the repository is refused**, loudly, with the message
`designate` uses: there is no root-relative identity for it, and inventing
one (say, the absolute path) is the duplication this change exists to end.
Outside any repository, nothing changes: identity is the path as typed.

Chunks that were ingested under an absolute or subdirectory path before this
change live under the old tag, and the first run after it sees them as new.
That is exactly what the twin warning is for, and the two land together.

### The twin warning

Inside `ingest_file`, on the entirely-new branch only - no live chunks under
this `src:` tag - the service asks the store for the project's live anchors
and looks for one whose `src:` path has the same final component. An anchor
is an `INGESTED` or `ARCHIVED` entry with a `src:` tag and no `sec:` tag;
that absence is what already distinguishes it from a chunk. A match is
appended to `Report.twins` as `(new path, existing path, live chunk count)`,
where the count is the anchor's file's live chunks via `_live_chunks`.

The CLI prints one line per twin to stderr and exits 0: a twin is a
question, not a failure. The refresh records twins in the run row, since an
automatic run is where one appears with nobody watching, and `reingest
status` renders them.

**A new store method, `anchors(owner_id, project) -> list[Entry]`**, one SQL
query over the tags array: live, in the project, has an element with the
`src:` prefix, has none with the `sec:` prefix. `search` cannot express "has
a tag with this prefix and lacks one with that prefix", and pulling every
chunk in the project through it to filter in Python would meet `Query.limit`
on any project with a few hundred chunks - which is every project this was
built for. No new index: the query runs once per entirely-new file, and a
project has tens of anchors.

**Nothing is superseded automatically.** A moved file and a renamed copy look
identical from here, and the user knows which. When it is a move, the fix is
to ingest the new path - which this run already did - and retire the old
chunks by hand. A `--from` rename flag is out of scope.

## Architecture

| file | change |
|---|---|
| `backends/postgres/migrations/017_ingest_runs.sql` | the table and its index |
| `domain.py` | `IngestRun` dataclass; `IngestTrigger` StrEnum (`AUTO`, `MANUAL`) |
| `store.py` | `start_ingest_run`, `finish_ingest_run`, `latest_ingest_run(owner_id, project)`, `anchors(owner_id, project)` on the Protocol |
| `backends/postgres/store.py` | the four implementations |
| `services/ingest.py` | `Report.twins`; twin check in `ingest_file`; `relative_to_root(paths, toplevel)` - the pure argument translation, raising `BadDesignation` for a path outside the repository; `refresh` and a new `ingest_manual` both open and finalise a run row; `status(store, owner_id, project, root)` returning an `IngestStatus`; `advisories(store, owner_id, root)`; `render_status` / `status_to_dict` |
| `project.py` | `toplevel(start) -> Path | None` |
| `services/events.py` | `StatusReport.ingest_advisories`, rendered and in `to_dict` |
| `cli.py` | `ingest` calls `relative_to_root` when `project.toplevel()` is not `None` and prints twins; `reingest status` renders the status and takes `--json`; `record status` passes the ingest advisories |
| `CLAUDE.md` | a paragraph under "Ingested documents" |

`ingest_manual` exists so the run-row bracket lives in the service for both
triggers. `ingest_paths` itself stays row-free, because `refresh` calls it
once per designation half and must wrap the whole loop in one row.

## Error handling

- `reingest run` keeps its contract unchanged: exit 0 on every path, nothing
  on stdout, stderr behind `REMEM_HOOK_DEBUG`. The run row is the only new
  side effect, and a failure to write it lands in the existing
  `BaseException` guard.
- `remem ingest` stays fail-loud. Failures exit 1 as today. A twin warning
  does not change the exit code. An argument outside the repository exits 1
  before anything is written.
- `reingest status`, `record status` and `advisories` are read-only. The
  on-disk check is `Path.exists()` against a root, wrapped so an unreadable
  directory reads as missing rather than as a crash.
- The advisory computation in `cli.py` is wrapped like the doctor call: an
  exception there yields no advisory, never a failed status command.

## Testing

Pure, no `db` marker, runs on CI:

- the twin matcher, given a list of anchor paths and a new path: same
  filename matches, same stem with a different extension does not, the
  file's own path does not match itself
- the argument translation: relative from the root unchanged, relative from
  a subdirectory rewritten, absolute inside the repository rewritten, outside
  the repository refused, outside any repository unchanged
- `render_status` for all four last-run states, with and without failures,
  twins and an embed error, and both spellings of the on-disk check

`db` marked:

- `refresh` writes one row with the right counts and `finished_at` set
- an ingest that raises mid-loop leaves a row with `finished_at` null
- a designated path missing on disk lands in the row's `failures`
- `anchors` returns only anchors, only live ones, only the owner's - with a
  second principal's anchors present and asserted absent by id
- the twin check does not fire against another principal's chunks
- `advisories` with a failed run for one project and a clean run for another
  returns exactly one line, naming the failed one and its pointer
- `remem ingest` writes a row with `trigger='manual'`
- `remem ingest` from a subdirectory supersedes rather than duplicates a
  chunk first ingested from the root

Every guard is watched failing first, per the stored rule: revert the defect
it targets on a scratch copy and confirm red.

## Out of scope

- Pruning run rows.
- Superseding twins automatically, or a `--from` rename flag.
- Storing a working directory on `ingest_settings`.
- Anything in the SessionStart context block or in `remem doctor`.
- Changing where the refresh reads files from (`repo_root()`, the main
  checkout even in a worktree) - existing behaviour, unrelated to this.
