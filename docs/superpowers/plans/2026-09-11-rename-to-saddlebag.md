# Rename remem to saddlebag

remem becomes **saddlebag**, the thing you carry what you need in, named to
pair with `saddle`, the container setup. The CLI command becomes `bag`.

Done before 0.9.0 deliberately: the `remem.agents` entry point is named as a
stable surface in the README's new compatibility section, so moving it after a
release costs a major version and moving it now costs nothing. Nothing
third-party implements it yet.

## Decisions

| | |
|---|---|
| PyPI package | `saddlebag` (free; npm is held by an abandoned 2020 stub, not pursued) |
| console script | `bag` |
| entry point group | `saddlebag.agents` |
| env prefix | `BAG_*` |
| database and role | renamed in place with `ALTER`, no dump |
| this checkout | renamed, and its project rows re-homed |
| `docs/superpowers/` | **left alone** - it records what was decided under the name it had |

`mem:` and `cmem:` tag namespaces do not contain the old name and do not
change. No schema migration is needed for them, or for anything else: the data
work is an UPDATE, not a DDL change.

## The two things that break silently

**1. The hooks.** Four entries in `~/.claude/settings.json` invoke bare
`remem`, plus Cursor's `hooks.json` and opencode's generated `remem.js`. The
moment the console script becomes `bag` they point at a command that is not
there, and because every hook is fail-soft by hard contract they fail with a
zero exit and no output - context injection, event recording, and the spawned
extraction/ingest/memory refreshes all stop, silently. `install` rewrites them,
but nothing reminds you to run it.

**2. The editable install.** `uv tool install --editable` resolves to
`/Users/brandon/llmworkspace/remem`, so the rename goes live in every session
on this machine the moment the branch is checked out *here* - not when it is
merged. This is why step 1 is a separate worktree.

## Steps

### 1. Work in a separate worktree

`git worktree add ../saddlebag-rename -b rename-to-saddlebag`. The live install
keeps pointing at this checkout and keeps working throughout. Nothing below
touches the running setup until step 6.

### 2. The code rename

- `src/remem/` -> `src/saddlebag/`, imports throughout.
- `pyproject.toml`: `name`, `[project.scripts] bag = "saddlebag.cli:app"`,
  `[project.entry-points."saddlebag.agents"]`, the three adapter paths, and the
  pyrefly/ruff config paths.
- 18 `REMEM_*` vars -> `BAG_*`. `REMEM_EXTRACT_CHILD` -> `BAG_EXTRACT_CHILD`
  **on all three hook sides in this same commit** - a half-rename leaves the
  extractor's own child recording events that the next extraction reads,
  without bound. `REMEM_CAPTURE_MODEL`/`REMEM_CAPTURE_CHILD` are already
  deprecation shims; they keep warning, naming the new spelling.
- Generated, machine-owned files: opencode's `remem.js` -> `bag.js`, Cursor's
  `remem.mdc` -> `bag.mdc`, and the `.git/info/exclude` line that names the
  `.mdc`. Both are overwritten unconditionally by `install()`, so no merge
  logic is involved - but a stale `remem.js`/`remem.mdc` left on disk would be
  loaded by its harness forever, so `install()` removes the old name.
- `compose.yaml`, `Makefile`, `.gitlab-ci.yml`, `README.md`, `CLAUDE.md`,
  `CHANGELOG.md`. Not `docs/superpowers/`.
- `DEFAULT_DSN` -> `postgresql://saddlebag:saddlebag@localhost:5433/saddlebag`.

Verification: `make check` green in the worktree against a **scratch**
database, 0 pyrefly errors, 0 skips.

### 3. Rehearse the data migration against a copy

Never against the live database first. `createdb` a copy from a dump, point a
throwaway config at it, run the script in step 4, and confirm the counts below
land. This is the project's own rule for destructive automation, and the delete
gate it exists to protect is the memory sync's.

### 4. The data migration

One transaction. Current live counts, which the script asserts before and
after:

| table | rows at `project='remem'` |
|---|---|
| `entries` | 952 |
| `events` | 4876 |
| `ingest_runs` | 117 |
| `memory_runs` | 72 |
| `extract_jobs` | 28 |
| `collections` | 2 |
| `ingest_settings` | 2 |
| `memory_settings` | 1 |
| `record_settings` | 1 |
| `capture_jobs_legacy` | 0 |

Plus, and these are the ones a plain `update ... set project=` misses:

- `collections.slug`: `remem` -> `saddlebag`, `remem-memory` ->
  `saddlebag-memory`. **The SessionStart hook injects the knowledge base whose
  slug is exactly the session directory's name**, so if the slug does not
  follow the directory, injection returns nothing and says nothing.
- `collections.query` is `jsonb` holding `{"project": "remem", ...}`. A smart
  collection whose query still names the old project matches nothing, forever.

Not changed: `src:` and `sec:` tags containing "remem" are ingested-document
identity for files under `docs/`, which this rename leaves alone. Renaming them
would orphan every chunk against its anchor for no gain.

Then rename the database and role, which needs no dump:

```sql
ALTER DATABASE remem RENAME TO saddlebag;
ALTER ROLE remem RENAME TO saddlebag;
```

`compose.yaml`'s `POSTGRES_USER`/`POSTGRES_DB` change to match.

### 5. Rename the checkout

`~/llmworkspace/remem` -> `~/llmworkspace/saddlebag`, which is what makes
`resolve_project` return `saddlebag` - it is the repo root's directory name and
nothing else.

Order matters: step 4 before step 5. Between them the directory still resolves
to `remem` while the rows say `saddlebag`, which reads as an empty project
rather than as corruption, and is recoverable in either direction.

### 6. Re-install and re-register

`uv tool uninstall remem`, `uv tool install --editable .` from the renamed
directory, then `bag install` for each harness, which rewrites the four
`settings.json` entries, Cursor's `hooks.json`, and the generated plugin files.

Verification, and none of it is optional given both failures above are silent:

- `bag doctor` - reports every adapter and scope, non-zero only on a missing
  required hook.
- `bag verify` - proves the record/extract path round-trips. doctor and verify
  are a pair; neither subsumes the other.
- `bag record status` - the 8 designated projects still advise, and the
  knowledge base budget line still names `saddlebag` at ~7917/8000.
- A new session actually receives its context block.

### 7. Merge, tag, publish

Merge to `main`, bump `version` to `0.9.0`, tag, and let the publish job run -
which is item 2 of the release checklist and the first time that pipeline has
ever executed.

## Rollback

Steps 1-2 are a branch; discard it. Step 4 is one transaction against a
database whose pre-state is a dump taken in step 3. Step 5 is `mv` back. Step 6
is re-running the old `install`. Nothing here is one-way until step 7 publishes
to PyPI, which is the only genuinely irreversible action in the plan.
