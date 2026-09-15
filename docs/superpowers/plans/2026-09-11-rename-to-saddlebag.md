# Rename remem to saddlebag

remem becomes **saddlebag**, the thing you carry what you need in, named to
pair with `saddle`, the container setup. The CLI command becomes `bag`.

Done before 0.9.0 deliberately: the `remem.agents` entry point is named as a
stable surface in the README's compatibility section, so moving it after a
release costs a major version and moving it now costs nothing. Nothing
third-party implements it yet.

This plan was revised on 2026-09-15 after a scan of the live store, a clean
end-to-end rehearsal against a restored copy, and the code rename in a
worktree. The first draft missed several things; they are recorded where they
landed rather than in a changelog of the plan.

## The naming rule

One rule decides every occurrence, so no case is argued individually:

- **Typed by a person -> `bag`.** The console script, every `bag <subcommand>`
  invocation (in hooks, `_spawn` argv, plugin templates and prose), skill names
  (`bag`, `bag-prime`, `bag-handoff`, `bag-record`), and the env prefix `BAG_*`.
- **Everything else -> `saddlebag`.** Package and module, PyPI name, the
  `saddlebag.agents` entry point group, the MCP server name (tools become
  `mcp__saddlebag__*`), config and cache directories, database and role, the
  generated `saddlebag.js` and `saddlebag.mdc`, the `.saddlebag-sync.json`
  watermark and `.saddlebag-conflict.md` sidecar, and prose about the product.

## Never renamed

- `docs/superpowers/` - it records what was decided under the name it had.
- `src/saddlebag/backends/postgres/migrations/` - applied migrations are never
  edited. `migrate.py` identifies a migration by filename stem with no
  checksum, so moving the package directory is safe; the files are
  byte-identical.
- `remem_array_to_string_immutable` - a SQL function from migration 001, baked
  into the `entries.search` generated column. It keeps its name in every
  database forever.
- `tests/fixtures/` - verbatim real memory files, including two named
  `remem-*`.
- `remember`, `remember_tool`, `remembering` - they contain `remem`, and a blind
  substitution produces `saddlebagber`.
- The GitLab project path `nighthawk-oss/remem` in URLs. Renaming the GitLab
  project is an outward-facing decision for step 7, and GitLab redirects old
  paths after a rename - not new paths before one.
- `mem:` and `cmem:` tag namespaces. Neither contains the old name.

## Things the rename script got wrong, and why

Worth keeping because each is a trap the next rename will fall into:

- **`import` is a subcommand.** `bag import claude-mem` is real, so the
  invocation rule matched Python's own `from remem import X` and wrote
  `from bag import X` - 39 type errors.
- **A template is not a literal.** `plugin.js` runs `` $`remem ${args}` ``;
  `${args}` is not a subcommand the invocation rule could see, so it became
  `saddlebag` - a plugin calling a binary that does not exist. A substitution
  applied identically to code and tests keeps the suite green while the real
  invocation is wrong, so every executable context was audited by hand.
- **Prose about the binary.** "puts `remem` on PATH", "registered as bare
  `remem`" mean the executable and became `bag`; "keeps saddlebag installable"
  means the package and stayed.
- **A positional string is not always argv.** The argv rule matched any
  `"remem"` followed by another string argument, so
  `events_for_session(owner, "remem", "claude-code", "s1")` became a query for
  project `"bag"` against rows written as `saddlebag` - 16 failures. Only a
  `"bag"` that opens a list is argv.
- **Renaming lengthens lines.** One string went past 88, and ruff does not
  break strings.

## A defect the rename surfaced

34 tests in three files had been round-tripping through the developer's own
live store on every run. install() folds verify()'s live round-trip into its
report, and `verify.round_trip` falls back to `os.environ` when given no env,
which names no DSN on a developer machine - so it resolved to the default
address. 30 were in `test_claude_code_install.py`, 3 in `test_capture_cli.py`,
and 1 in `test_cursor_install.py`. It is the likeliest source of the disabled
`__remem_verify__` row found in the live store.

Every one of them passed. A failed round-trip is a warning in the install
report and no test asserted it succeeded, so a green run said nothing about
where the round-trip went. It surfaced only because the renamed default role
did not exist yet, and one test that *did* assert on the round-trip failed
authenticating as it.

The scope was measured rather than inferred: a throwaway pytest plugin wrapped
`psycopg.connect` and named every test that connected as the default role,
which in the rename worktree differs from every test DSN's. Its one false
positive is `test_cli.py`'s unreachable-Postgres test, which uses that role
deliberately against port 1.

The opencode and claude-code events install tests already carried a fixture
preventing this, with a comment warning about exactly this failure; the other
three files never had it. `test_claude_code_install.py` gets an autouse
fixture gated on the `db` marker, since the defect was thirty tests each
forgetting the same parameter, and four tests that relocate `CLAUDE_CONFIG_DIR`
through an explicit env merge its settings in.

## Existing installs

Both adapters already carry a `LEGACY_COMMANDS` table, built for exactly this:
`install()` rewrites a command it once wrote in place rather than appending
beside it, and `hook_state()` reports it STALE rather than missing until it
does. Cursor's table was empty with a comment saying no command had been
renamed yet. The current `remem ...` spellings go into both tables, so `bag
install` repairs four Claude Code hook entries and four Cursor ones with no
hand-editing of JSON.

The MCP registration has no such mechanism - `install()` sets its key and
never removes one - so a stale `mcpServers["remem"]` would linger as a server
that fails on every startup. `install()` removes it, scoped to an entry whose
command is the `remem` remem itself wrote.

Skills are copied, never removed, so the four old `remem*` skill directories
and the old `.cursor/rules/remem.mdc` are deleted by hand during cutover,
after backup.

## The data migration

Scoped by scanning every text, jsonb and text[] column of a restored copy for
`remem`, `remem-memory`, `__remem_verify__` and the checkout path - not by a
list of tables, which missed three of these.

**Moved** (identity and display):

| column | what |
|---|---|
| `project` in `entries`, `events`, `extract_jobs`, `ingest_runs`, `ingest_settings`, `memory_runs`, `memory_settings`, `record_settings`, `collections`, `capture_jobs_legacy` | the project key |
| `record_settings.project = '__remem_verify__'` | verify's disabled scratch marker |
| `collections.slug` | `remem` and `remem-memory`. The SessionStart hook injects the knowledge base **whose slug is exactly the session directory's name**, so a slug that does not follow the directory injects nothing and says nothing |
| `collections.query` jsonb `project` | a smart query naming the old project matches nothing, forever |
| `collections.title`, `.description` | the `# remem` heading the context block opens with, and a description naming the old memory directory slug |
| `memory_settings.collection_slug` | a slug reference, not a project key |
| `memory_settings.working_dir` | the absolute checkout path |

**Left alone** (content and history): 253 entry titles, 660 bodies and 4
summaries that mention remem, 10 entries tagged `remem`, and every event
payload's recorded cwd. Rewriting knowledge text or recorded history in bulk is
exactly what must not happen silently. Rule titles in the injected block will
still say "remem" in places; that is a follow-up for a person, not this script.

Counts are **measured at run time and asserted**, never taken from this
document: `events` grew by 24 during the session that wrote it, because that
session was recording itself. `migrate-data.sql` refuses to merge into a name
already in use, asserts every table's count moved intact, and asserts nothing
structural remains at an old name.

The role rename needs a **temporary second superuser**: `remem` is the
container's only one and a session cannot rename its own role. The password is
SCRAM, which survives a rename (MD5 would not), and is reset anyway because the
DSN names the new role.

Rehearsed end to end, from a fresh restore, with the exact two files that run
live: every count moved, both collections retitled, `saddlebag` the only
superuser, full-text search through the preserved function returning results,
1,018 vectors intact, and the old credentials refused.

## Outside the database

- **Compose volume.** Compose names volumes `<project>_<key>` and defaults the
  project to the directory, which is how five stray volumes accumulated from old
  worktrees, and how renaming the checkout would have pointed compose at a fresh
  empty volume that looks exactly like data loss. `compose.yaml` now pins
  `name: saddlebag`.
- **The old volume is copied, not migrated.** The container is stopped, the
  volume copied to `saddlebag_saddlebag-pgdata`, and the migration runs on the
  copy - so `remem_remem-pgdata` stays byte-for-byte pristine and rollback is
  starting the old container, not a restore.
- **Watermarks.** All eight designated projects' memory directories hold a
  `.remem-sync.json`. Its contents name memories and hashes, not the tool, so a
  plain rename is safe. Without it every file looks unseen and the sync's two
  safety gates stop working as designed.
- **The memory directory** for this checkout is keyed on its absolute path, so
  it is copied to the new slug. The original stays - the running session reads
  it.
- **Config.** `~/Library/Application Support/remem/config.toml` holds
  `max_chars = 8000`, and it is **copied** to `.../saddlebag/`. This was nearly
  missed twice: first by checking `~/.config/remem`, which is not where
  `platformdirs` puts config on macOS, and then by searching only environment
  variables. Without it the renamed code falls back to the default 6000, the
  knowledge base (7921 chars) is over budget, and context injection dies in
  every session with a zero exit and no output. It was caught by running the
  renamed CLI against the migrated rehearsal copy, whose budget advisory said
  so. Copied rather than moved so a rollback to the old tool keeps its config.
- **Cache.** `~/Library/Caches/remem` moves; losing it would only re-warn.
- No permission rule or `REMEM_*` variable exists in either user settings file,
  the shell environment, or any dotfile.

## Outside this repository

Swept before cutover, because a rename's worst failures are in things that
call it by name and that its own tests cannot see.

- **saddle depends on it at runtime.** `~/.config/saddle/profiles/go.yaml`
  carries `skills: [remem]` - bind-mounting `~/.claude/skills/remem` into
  containers - and an MCP entry that spawns `remem serve --http`. With the
  binary gone and that skill directory removed, every saddle session would fail
  to start its host service and mount a path that does not exist. The cutover
  rewrites the profile to `bag` for the skill and the spawn and `saddlebag` for
  the MCP name. saddle's own README example and design docs still say remem;
  that is a follow-up in the saddle repository, not something this one edits.
- **`~/.claude/settings.json` is a symlink** into the dotfiles repo at
  `~/workspace/homedir`. A backup that renamed rather than copied would have
  moved the link aside and left `bag install` writing a regular file in its
  place - silently detaching live config from the repo that tracks it. It does
  not: `jsonfile.backup` uses `shutil.copy2` and `write_json` uses `write_text`,
  and both follow the link. Proved in a scratch `HOME`, not inferred - the rehearsal
  against real files had used copies, which could not have caught it. The
  dotfiles repo will show the four command changes, which are for committing
  there.
- **Nothing else calls it.** No crontab entry, launchd agent, shell alias or
  function, and no project- or user-scoped config for any other agent, beyond
  this checkout's own `.cursor/hooks.json`.
- **Four Claude Code sessions were open** when this was written, each with its
  own `remem serve`. Every one loses its MCP tools and hooks when the role is
  renamed, until restarted.

The install itself was rehearsed against copies of the real `settings.json`,
`.claude.json`, skills and `.cursor/hooks.json`, with its round-trip on the
migrated copy: every `remem` command was repaired in place, the stale MCP server
removed, and both files otherwise identical to what they were.

## Cutover

`cutover.sh`, one phase at a time, output read before the next:

1. `preflight` - read-only. Refuses unless `$NEW` is free, the worktree is
   clean, the target volume does not exist, nothing is connected to the
   database, and no detached `remem events|reingest|memory` job is running.
2. `backup` - a dump, and copies of `settings.json`, `.claude.json`, the old
   skills, `.cursor/`, the cache and all eight watermarks, outside the repo.
3. `db` - **uninstalls the old tool first.** While it is installed, any session
   that starts spawns a job against the database that can write
   `project='remem'` rows after the migration commits. Then stop, copy the
   volume, start the new container, migrate, rename the role.
4. `files` - watermarks, memory directory, config, cache, old skills, old
   cursor rule, and saddle's profile.
5. `merge` - land the branch, remove the worktree.
6. `install` - the new tool, then `bag install claude-code` and `bag install
   cursor --scope project`, which the legacy tables turn into in-place repairs.
7. `verify` - `bag db status`, `doctor`, `verify`, `record status`, `memory
   status`, that `settings.json` is still a symlink, the dotfiles diff, the
   saddle profile, and a grep of every live config file for leftovers.
8. `move` - rename the checkout and reinstall from the new path. **Last**,
   because this pulls the working directory out from under a running Claude
   Code session. Restart from `~/llmworkspace/saddlebag` and confirm the
   SessionStart block opens `# saddlebag`.

Everything before `move` rolls back by stopping the new container, starting the
old one against its untouched volume, reinstalling the old tool, and restoring
files from the backup.

## Then: tag and publish

Bump to `0.9.0`, tag, and let the publish job run. Two things outside this
repository have to happen first, and both are yours to do:

- **PyPI.** The trusted publisher is configured for project `remem`. A pending
  publisher for `saddlebag` has to be registered on pypi.org before the job can
  mint a token.
- **GitLab.** Whether the project path moves from `nighthawk-oss/remem`. The
  trusted publisher pins the repository path, so decide this before
  registering it.
