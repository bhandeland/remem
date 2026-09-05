# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
docker compose up -d           # Postgres 18 + pgvector on localhost:5433
uv sync
uv run pytest                  # full suite
uv run pytest tests/test_extraction_service.py::test_name   # one test
uv run pytest -m 'not db'      # skip everything that needs Postgres
uv tool install --editable .   # puts `remem` on PATH (see below)
remem db up                    # create the database and run migrations
remem db status                # applied vs pending migrations
```

DB-backed tests `pytest.skip` with an explanatory message when Postgres is unreachable - a
green run does not mean the DB tests ran. Check the skip count.

`uv tool install --editable .` is effectively mandatory for any work touching the Claude
Code integration: the MCP server and both hooks are registered as bare `remem`, and a
`remem` that only exists in the project venv produces a config that silently does nothing
(hooks are fail-soft, the MCP server never starts).

## Architecture

Strict layering, and the seams are deliberate. Each layer may call downward only:

```
frontends:  cli.py (Typer)  mcp_server.py (FastMCP)  agents/claude_code/hook.py
services:   services/{write,search,kb,record,extraction}.py <- every policy decision lives here
store:      store.py (Protocol)  -> backends/postgres/store.py (all SQL)
domain:     domain.py (pure dataclasses/enums, no I/O)
```

- **Frontends parse and format; they never decide.** A rule enforced in a service is one
  every frontend gets for free (query clamping, the search tier chain, record opt-in checks). If
  you find yourself adding a policy branch in `cli.py`, it belongs in `services/`.
- **`store.py` is the portability seam** - a `Protocol`, with Postgres as the only
  implementation. Ownership is enforced *inside* the store (`NotOwner`), not by callers.
- **`session.open_session()` is the only way to reach the database.** It connects,
  ensures the principal, and hands back a `Session`. It does **not** run migrations -
  `remem db up` is the only thing that applies them, and `remem db status` names what is
  pending. A schema behind the code therefore presents as a raw `UndefinedTable` from an
  ordinary command rather than as anything self-healing, so check `db status` before
  concluding a new feature is broken. Nothing outside
  `session.py`/`backends/` should import psycopg. `autocommit=True` is for long-running
  work that records its own progress (`remem events process`) - in a single transaction a failed
  statement poisons the connection and the final COMMIT becomes a ROLLBACK.
- **`agents/base.py` + `agents/registry.py` are the pluggability seam** - adapters register
  under the `remem.agents` entry point group and load lazily; a broken third-party adapter
  warns rather than breaking remem. Two adapters ship today: `claude-code` (an MCP
  registration plus four hook entries in settings the adapter owns and merges) and
  `opencode` (one generated file, `plugin.js`, dropped into a directory opencode scans).
  The contrast is deliberate - the second adapter proves the seam by looking nothing like
  the first.

### Data model

`Entry` (kind: note/doc/rule, origin: human/agent/extracted/handoff) is the unit of knowledge.
`Collection` ("knowledge base") membership is two things unioned: a smart `CollectionQuery`
(project + tags + kinds) plus explicitly pinned entries. An empty query matches *nothing*,
forever - `kb.advisories()` exists to say so at creation time.

Search is three tiers - exact full-text (`tsvector` generated column, weighted
title/body/tags), then semantic (pgvector cosine distance over `entry_vectors`), then
trigram similarity - and each runs **only when the one above returned nothing**, never
blended. Semantic sits above trigram because a query that matches nothing lexically is
far more often a different wording than a typo.

Every hit carries `Hit.match` (`Match.EXACT`/`SEMANTIC`/`FUZZY`) and every frontend must
surface it - `~`/`?` markers in the CLI, `"match"` in `--json` and in MCP `recall`. An
agent handed an unmarked approximate match cites it as certain. There is no compatibility
shim for the `Hit.fuzzy` boolean this replaced, and a test asserts its absence.

The embedder is an **optional dependency** (the `[embed]` extra) and vectors are written
only by `remem embed`. Missing either one costs the middle tier and nothing else: search
degrades to the two tiers it always had, silently and exiting 0. `remem embed` is the
opposite - fail-loud - because an unavailable embedder is its entire job failing.
`services/search.shared_embedder()` owns both halves of that policy, memoises the
embedder per model name, and is called **from inside the semantic tier**: constructing a
`LocalEmbedder` imports fastembed, builds an ONNX session and can download ~130MB, and a
search the exact tier answers must never pay for any of it. Frontends pass `embed_model`,
never an embedder.

### Migrations

Numbered `.sql` files in `backends/postgres/migrations/`, applied in filename order and
tracked in `schema_migrations`. Add a new numbered file; never edit an applied one.
`migrate()` runs inside the *caller's* transaction - the caller owns commit/rollback.
`applied_versions()` is deliberately read-only (inspecting a DB must not write to it).

Timestamps use `clock_timestamp()`, not `now()`: tests run inside one rolled-back
transaction, where `now()` gives every row an identical `created_at` and makes
`order by created_at desc` non-deterministic.

### Events and extraction

Raw per-tool-call events, recorded from a harness and extracted into entries later.
Supersedes capture - `remem capture enable|disable|status|drain` still work as hidden
aliases that warn once and delegate, for muscle memory and shell history, but the
real commands are `remem record` and `remem events`.

Recording is opt-in per project - that gate is the entire safety story, and it is
checked in `services/record.py`. Events are stored **in full** and kept
**indefinitely**; nothing prunes them on a schedule. `remem events prune --before`
is the only thing that ever deletes one, and only on request.

Flow: a harness hook (or plugin) does exactly one INSERT via `remem record event`
(everything fragile is deferred), and `remem events process` - run from cron, or
spawned by `hookio.spawn_process` at any harness's session start - extracts
entries from sessions that have gone quiet, via
`claude -p` in `extract/claude_cli.py`, writing entries with `origin='extracted'`
and `entry_events` provenance rows.

Extraction is triggered by **idleness**, not a session-end hook: a session is
extractable once it has unextracted events and none newer than
`REMEM_IDLE_MINUTES` (default 20). Two of the three harnesses remem targets have
no end-of-session hook, so a clock is the only trigger all of them share; a hook
that never fires would strand a session forever, while a clock always ticks. The
attempt-cap "gave up" rule lives in exactly one place, `extraction.awaiting_sessions`
- both `remem events process` and `remem record status` route through it, so they
cannot disagree about which sessions are stuck.

Invariants worth not breaking:
- `entry_events.event_id` has **no foreign key**, deliberately, and no read path may
  dereference it: pruning must be able to delete an event out from under its
  provenance row without touching `entries`, leaving the row visibly dangling rather
  than blocked or cascading. `covers_through` (not `entry_events`) is what makes
  "already extracted" answerable **per event** rather than per session.
- Extracted entries are **excluded from knowledge base context blocks** (`kb.resolve`
  filters `Origin.EXTRACTED`) so machine text never crowds out hand-written rules.
  They do appear in `search`/`recall`. Promote one with `remem kb pin`.
  `search.DEFAULT_ORIGINS` must gain any future origin or that origin silently
  vanishes from search.
- `extract/base.py` treats all model output as untrusted: shape-checked, capped
  (`MAX_ENTRIES`/`MAX_TITLE`/`MAX_BODY`), filtered before it reaches the store.
- The renderer spends its budget on **coverage before detail**. `render_events`
  hoists payload keys that are constant across the batch into one note, caps
  every oversized value (`MAX_FIELD_BYTES`, then `TIGHT_FIELD_BYTES`), and only
  then drops whole events off the front. Measured: 40KB of tail (18 of 112
  events) returned nothing in three runs, where the whole session with values
  capped returned entries in five of five. Both the hoist and the cap are keyed
  on the batch and on value size, never on a table of key names - the module
  renders Cursor and opencode events too, and their constants are different
  keys. A cut `tool_response` still says what the tool did; a dropped event
  says nothing.
- The extraction model is **pinned** (`REMEM_EXTRACT_MODEL`, default `sonnet`), not
  inherited from the session, so cost/behaviour do not drift. Haiku was measured and
  rejected on judgment, not JSON validity. `REMEM_CAPTURE_MODEL` is read for one
  release and warns to stderr naming the replacement - never both silently.
- `CHILD_ENV_VAR` (`REMEM_EXTRACT_CHILD`) is set on the spawned `claude -p` so its own
  hooks refuse to recurse. Three hooks check it. **Rename it on every side in the
  same commit or not at all** - renaming one side leaves the extractor's own child
  recording events, which the next extraction reads, without bound.
- Jobs stop retrying after `MAX_ATTEMPTS`; `remem events process --job ID` retries by
  id. Failures record the reason *and* the model's raw output, both separately
  truncated.
- `install()` performs a live database round-trip (it proves the record/extract
  path actually works), which is why the install tests are marked `db`.

### Checking the install

`remem doctor` answers the one question a fail-soft pipeline cannot ask
itself: **does the installed config actually register the hooks this adapter
installs?** It reads files and opens no database - a diagnostic that needs
the system healthy is no use when it is not.

Each adapter answers with facts (`hook_state()`, a probed optional
capability returning `HookState`); `services/doctor.py` makes every
judgement, so all adapters agree on what "missing" means and one
computation feeds `remem doctor`, its `--json`, and one advisory line in
`remem record status`.

Three rules worth not breaking:

- **One table per adapter.** `HOOK_ENTRIES` is read by both `install()` and
  `hook_state()`. Two tables kept in step would drift, and drift is the
  whole bug: settings.json held three hooks for the life of the events
  pipeline and nothing could see it.
- **Unchecked never renders as `ok`.** opencode ships a plugin file rather
  than hook configuration, so it does not implement `hook_state()` and the
  report says "no hook registration to check". Reporting success for
  something never verified is the failure this command exists to catch.
- **Only a missing *required* hook exits non-zero.** `STALE` and
  `DUPLICATED` still fire the hook; `UNCHECKED` reports the absence of a
  check. Exiting non-zero for "I could not tell" trains people to ignore
  the exit code.
- **No confident answer about a scope that was not examined.** With no
  `--scope`, `check()` sweeps `SCOPES` (remem's own constant - the
  vocabulary is closed and every adapter hardcodes it; `UnsupportedScope`
  is the skip signal) and reports one row per *(adapter, scope) that is
  installed*, so a half-install in one scope cannot hide behind a healthy
  other. "Not installed" is said once per adapter and **names every path it
  looked at**. With `--scope X` the question is exactly X, and
  `UnsupportedScope` stays `UNCHECKED`-with-a-warning rather than becoming
  a skip - sweeping there would make `remem doctor claude-code --scope
  project` print nothing and exit 0. This is not hypothetical tidiness:
  defaulting to user scope made `remem doctor` report cursor "not
  installed" on the machine where cursor was installed at project scope and
  recording events. Every pointer carries its scope too - the advisory line
  and the `Fix:` line both - because a pointer that leads to a
  contradictory screen teaches the user the line lies.

`doctor` and `verify` are a pair and neither subsumes the other: doctor asks
whether the harness will ever call remem, `verify` asks whether remem works
when called.

### Handoffs

A handoff is an `Entry` with `origin='handoff'`, `kind=doc`, and a `topic:<slug>`
tag - no separate table. Invariants:

- Writing one supersedes the prior live handoff for the same `(project, topic)`.
  Only the newest is ever live; the chain is the history.
- Excluded from context blocks (`kb.resolve` filters origins) and from search
  unless `include_handoffs=True`. `search.DEFAULT_ORIGINS` must gain any future
  origin or that origin silently vanishes from search.
- `remem handoff write` is fail-loud, unlike every hook in this repo: the user
  is about to `/clear`.
- `session_size.py` is Postgres-free and agent-neutral; it runs on every user
  prompt via the `UserPromptSubmit` hook. Warn state lives in the platform
  cache dir and fails toward warning, never toward silence.

### Ingested documents

`remem ingest <path>` loads markdown in as one entry per `h1`-`h3` heading,
plus an anchor entry per file. Identity is two tags, `src:<path>` and
`sec:<slug>`, so re-ingest is idempotent: unchanged sections are skipped
without a write, edited ones supersede their previous version, and sections
that vanished from the file are superseded **by that file's anchor** -
`set_superseded` needs a replacement id and a deleted heading has none. The
sweep calls `store.set_superseded` directly rather than `write.supersede`,
which would create a replacement the orphan does not have.

Splitting is on headings and only on headings. A size-based sub-splitter
would cut through fenced code, which is most of what a plan contains. The
only fence logic in `markdown.py` is a boolean for heading detection, so a
`#` comment inside a code block is not mistaken for a section.

Chunk titles come from the document's opening **`h1`**, falling back to the
filename stem when a file has none - "Ingest design § Decisions" rather than
"2026-09-01-doc-ingest-design § Decisions". Only an h1 that opens the file
counts; one further down is an ordinary section, since taking the title from it
would rename the document halfway through. The stem still IDENTIFIES the
document - a headingless file's `sec:` slug is its stem - so `split` takes
`doc_name` and separates naming from identity the same way `ingest_file`'s
`root` separates where a file is read from what identifies it. Retitling a
document therefore never duplicates its chunks.

Because the title is half the embedding text and the highest-weighted field in
the tsvector, "changed" compares **title and body**, not body alone. A renamed
document supersedes every one of its chunks on the next ingest; comparing
bodies alone would leave the old titles standing until each section's prose
happened to change.

Two origins, because `search.DEFAULT_ORIGINS` is an allowlist and an
exclude filter was deliberately declined: `INGESTED` (specs, notes,
decisions) is in that list, `ARCHIVED` (plans, written with `--archive`) is
not and needs `--archived`. Plans are the minority by count (119 chunks
against 211) and three times the volume, and their bulk is source code that
now lives in `src/`. `DEFAULT_ORIGINS` must gain any future origin or that
origin silently vanishes from search.

`markdown.py` is pure - no I/O, no store - so its tests carry no `db`
marker and run on CI. Whether a chunk changed is answered by comparing
bodies, not by a stored hash.

Re-ingest also runs **automatically**, because manual meant it drifted:
two days of doc writing once left 32 chunks unindexed. `remem reingest
designate <paths> [--archive]` records which paths a project re-ingests
(migration 016), and `hookio.spawn_ingest` starts a detached `remem
reingest run` from the same two places `spawn_process` starts extraction -
Claude Code's `SessionStart` and `remem hook context` - which is the one
trigger all three harnesses share. Fixed once, not per install path.

- These are **not** subcommands of `ingest`. `remem ingest <path>` is a
  bare command taking positional paths, so a sub-app of that name cannot
  coexist with it, and breaking the documented manual command to make room
  for the automatic one is the wrong trade. `ingest` and `embed` keep their
  fail-loud contracts - a person asked for those.
- The designation holds a value rather than a boolean, like
  `memory designate`, with `archive` **in the primary key**: the refresh is
  genuinely two invocations with different origins, so a project has at
  most two rows and they clear independently. No row means the spawned run
  does nothing, and that silence is the entire opt-in.
- Paths are stored **repo-relative** and resolved against the git root at
  run time. Unlike `memory_settings` this needs no recorded working
  directory - ingest already resolves its project from the git common dir,
  so a worktree and its main checkout share both project and relative
  paths. `designate` refuses an absolute path or a `..` escape, loudly,
  because that is the one moment there is a human to tell.
- `reingest run` is fail-soft in the strongest form this repo has: it exits
  0 on every path, prints nothing to stdout, and explains itself only to
  stderr behind `REMEM_HOOK_DEBUG`. It is the one place in `cli.py` that
  catches `BaseException` - `_session` turns an unreachable database into
  `typer.Exit(1)`, a `SystemExit` that would otherwise sail past
  `except Exception` and out of a hook-spawned command as a non-zero exit.
- `services.embed.backfill_if_pending` exists so the common case costs
  nothing. `backfill` takes an Embedder already built, which is right when
  a user asked for it; here the backlog is empty almost every time, and
  constructing a `LocalEmbedder` imports fastembed, builds an ONNX session
  and can download ~130MB. The model **name** is enough to ask whether
  there is work, which is what makes the check possible before the cost -
  the same policy `services.search.shared_embedder` applies inside the
  semantic tier. It returns `None` for "nothing to do", distinct from an
  `EmbedResult` with `embedded=0`, and lets `load` raise so the caller
  decides whether an absent embedder is fatal.
- An unavailable embedder loses the semantic tier and nothing else, so
  `refresh` records it in `embed_error` and keeps the entries it wrote.
  `remem embed` still exits 1 there, on purpose.

### Claude Code memory

`remem memory sync` owns `~/.claude/projects/<cwd-slug>/memory/` - Claude
Code's file-based memory - as a generated view of a designated collection.
Unlike opencode's `remem.js` and cursor's `remem.mdc`, this generated file
set has a second writer that cannot be told to stop, so the sync **adopts
before it regenerates**: anything on disk remem has not seen becomes an
entry first.

- Opt-in per project, holding a value rather than a boolean: which
  collection. An undesignated project generates nothing, which is what
  keeps `MEMORY.md` from double-loading against the `SessionStart` block.
- The designation also records the **working directory it was made from**
  (migration 015), because the two halves are keyed on different things:
  the designation on the project, the memory directory on the absolute
  cwd. Neither derives the other - a worktree and its main checkout share
  a project and have two memory directories - so `remem memory sync --all`
  is only expressible because the answer is stored. `designate` therefore
  refuses a `--project` naming anything but the current directory's
  project: recording a working directory that has nothing to do with the
  designation would surface much later, as a sync writing to the wrong
  place. Rows written before 015 read back as `None` and `--all` skips
  them by name rather than guessing; re-designating is the fix, and the
  skip exits non-zero because a directory that was not synced is a
  definite statement, not an "I could not tell".
- `.remem-sync.json` is what makes "which side moved" answerable. This is
  deliberately the opposite of `ingest`, which compares bodies and stores no
  hash - ingest has one writer, so "differs" and "the file changed" are the
  same statement. Here both sides write.
- Two gates and they are the whole safety story: a file whose checksum does
  not match its watermark is never deleted, and a file changed on both sides
  is never overwritten. Conflicts write remem's version alongside as
  `<name>.remem-conflict.md` and exit non-zero.
- Identity is the **filename stem**, recorded as a `mem:<name>` tag. An
  entry written by hand has no such tag, so the export mints one from the
  title and writes it back before the cases run - without that pre-pass the
  export is only ever what the sync adopted off disk, which is silently the
  "directory owns it, remem ingests" design the spec rejected. The
  frontmatter `name:` is the user's field, carried through a regenerate
  rather than rewritten; a file renamed on disk therefore mints a new entry
  and the old name is regenerated.
- A collection that resolves at `kb.RESOLVE_LIMIT` refuses to sync. Past the
  cap an entry remem cannot see is indistinguishable from one that left the
  collection, and the delete gate would pass.
- `memory_file.py` is pure, so its tests carry no `db` marker and run on CI.
  The round trip is load-bearing rather than cosmetic, but whole-file byte
  equality is not the property - a real fixture disproved it. What the sync
  needs is that the **body** round-trips byte for byte (the watermark hashes
  the body alone, so frontmatter whitespace can never read as a content
  change), that `render` is idempotent, and that no `metadata:` key parse
  saw is ever dropped.
- The generated `MEMORY.md` uses an em dash between link and hook, against
  this repo's convention, because that line's format belongs to Claude Code.
- Not in `remem doctor`: the designation lives in the database and doctor
  opens no connection. The overlap count lives in `remem memory status`,
  along with a count of `.remem-conflict.md` sidecars still on disk from a
  past sync - nothing ever deletes one automatically, since doing so risks
  destroying the copy the user needs, so `status` is what keeps an
  unresolved conflict from going unnoticed between syncs.

### Settings

`remem config` reads and writes two files from one command, routed by key
name: `REMEM_*` keys land in remem's `config.toml`, a curated set of Claude
Code environment variables lands in the `env` block of `settings.json`.

- The table of Claude Code variables lives on the **adapter**
  (`agents/claude_code/env_vars.py`), not in `services/`. It is a fact about
  Claude Code, not about remem, and keeping it there is what lets a future
  adapter ship its own.
- `env_settings()` and `settings_path()` are **optional adapter capabilities**,
  probed with `getattr` in `settings.resolve_targets` and documented on the
  Protocol rather than declared on it. They go together: an adapter with only
  one of them reports "no settable env vars" rather than falling back to
  another agent's file. The frontend resolves `--agent` and nothing else.
  A capability that *raises* lands where a missing one lands - warn, degrade to
  the remem half, and keep going. Same contract as `agents/registry.discover`:
  a broken third-party adapter must never be why `remem config` will not run.
- **No credential and no endpoint variable is ever settable.** Their absence
  from the table is the enforcement; `tests/test_env_vars.py` asserts it.
  Neither is `CLAUDE_CONFIG_DIR` or `REMEM_CONFIG` - each names the file that
  would store it.
- The two targets resolve in **opposite directions**: the environment beats
  remem's `config.toml`, while `settings.json` beats a shell export. `set`
  says so when the key it just wrote is also exported, because writing a
  shadowed remem key is otherwise a silent no-op.
- **Every write backs the file up first and says where the backup went.** A
  rewrite of `config.toml` loses comments and formatting, so the writers
  return the backup path and the CLI echoes it - a `.bak<timestamp>` nobody
  is told about is barely a safety net.
- This is the one service that opens no database connection. It must keep
  working with Postgres down.

### Hooks are fail-soft, and that is a hard contract

`hook.session_start` / `session_end` exit 0 unconditionally, print nothing on error, and
never raise. A knowledge tool must never be why a session will not start. Because silence
is ambiguous, `REMEM_HOOK_DEBUG=1` writes the reason to **stderr** - stdout is the context
block and nothing else.

The SessionStart hook injects the knowledge base whose slug is exactly the session
directory's name. Writes, by contrast, resolve `--project` from the git repository root
(`project.resolve_project`, via `--git-common-dir`) so subdirectories and worktrees file
under the repository they belong to.

### The opencode adapter

`plugin.js` (`src/remem/agents/opencode/plugin.js`) is hand-written and shipped as
package data; the INSTALLED copy - `remem.js`, in the directory `plugin_dir` names for
the chosen scope - is what is generated and machine-owned. `install()` overwrites that
installed copy unconditionally, no merge, no version marker, no prompt. It imports
nothing, because `$` arrives on `PluginInput`: there is no npm dependency to install,
pin, or keep in step with opencode's own releases. A user who wants local edits to
`remem.js` is asking for the wrong file - remem owns it.

opencode has no session-start hook, so `remem hook context --agent <name>` exists as the
harness-neutral half of what `hook.session_start` does for Claude Code: given whatever
payload a harness has on hand, the adapter's `identity()` turns it into a project and the
command prints the knowledge base block for that session, fail-soft like every other
hook. The opencode plugin calls it from `experimental.chat.system.transform`, which fires
on every message - the plugin holds an in-process `Set` of session ids so the block is
fetched once per session, not once per turn, the same bargain Claude Code's SessionStart
makes by construction.

The hook-contract test (`tests/test_opencode_hooks_contract.py`) is deliberately two
tests, not one: `test_the_plugin_subscribes_only_to_hooks_opencode_emits` always runs, on
CI and everywhere else, and catches `plugin.js` subscribing to a hook name opencode does
not emit - the exact failure mode of a competing tool's opencode integration that reported
success for months while recording nothing. `test_the_vendored_list_still_matches_the_installed_types`
is marked `@pytest.mark.opencode` and may skip; it only guards the freshness of remem's own
vendored copy of opencode's `Hooks` interface (`HOOK_NAMES`, `PLUGIN_TYPES_VERSION`), which
needs opencode's plugin types installed to check. Collapsing them into one test would
produce a guard that skips on CI - the same failure mode the `db` markers already taught
this project to distrust.

### The cursor adapter

`src/remem/agents/cursor/`, four modules and no generated script of any kind -
`hooks.json` names the `remem` command directly, because `remem record event`
already reads its payload as JSON on stdin. It sits between the two adapters
that shipped before it and deliberately borrows from each: `.cursor/hooks.json`
is user-owned and shared - other tools write there too - so it gets Claude
Code's treatment (read, merge, back up first, echo the backup path), while the
generated `.cursor/rules/remem.mdc` is machine-owned and overwritten
unconditionally, like opencode's `remem.js`. A user who wants local edits to
the `.mdc` is asking for the wrong file.

Cursor emits 21 hooks (vendored in `agents/cursor/hooks.py`, read out of
`Cursor.app`'s minified bundle); this adapter subscribes to exactly four:
`sessionStart` (injects, via `remem hook context --agent cursor`),
`postToolUse` (`EventKind.TOOL_CALL`), and `beforeSubmitPrompt` /
`afterAgentResponse` (both `EventKind.MESSAGE`). Six hooks **block** - Cursor
waits on them for a permission decision on their stdout
(`beforeShellExecution`, `beforeMCPExecution`, `beforeReadFile`,
`beforeTabFileRead`, `subagentStart`, `preToolUse`) - and this adapter is
deliberately wired to none of them, checked in as `hooks.BLOCKING_HOOKS` so an
edit that reaches for one fails a test instead of shipping a hook that can
deny a permission by failing. The remaining exclusions are `afterAgentThought`
(reasoning text: high-volume, low-signal for extraction) and the specific
`afterShellExecution`/`afterMCPExecution`/`afterFileEdit` hooks, folded into
the generic `postToolUse` instead - one parser instead of three, and no gap
opens when Cursor adds a tool type.

**Injection is one of seven optional adapter capabilities** -
`inject(self, block, payload) -> str | None`, probed with `getattr` exactly as
`event()`, `env_settings()`, `settings_path()`, `verify()`, `hook_state()` and
`memory_dir()` are (the full seven, with the reasoning for each, are the comment block on
`agents/base.py`'s Protocol - keep the count there and here in step, because
an author who learns a capability exists by accident is how the missing
`PostToolUse` survived). Cursor needs the
context block written to a file rather than printed or returned, so `cli.py`
never has to know what an `.mdc` is; Claude Code simply does not implement
`inject()`. This is the one probed capability where a **missing**
implementation is the default, not a degradation - printing to stdout at
`SessionStart` is exactly what Claude Code is supposed to do, not a fallback
from something richer. `sessionStart` fires once, so the `.mdc` is written
once per session by construction, with no state to keep anywhere - the third
time this project has needed "once per session" and the third different
mechanism: Claude Code gets it free from `SessionStart`, opencode keeps an
in-process `Set` of session ids because it has no session hook at all, and
Cursor needed neither once the stale "no `SessionStart` equivalent" premise
from the events-and-recall spec was corrected.

Writing the `.mdc` is also the first time remem puts context into the user's
**working tree** rather than a stream, which is why `agents/cursor/rules.py`
adds `.cursor/rules/remem.mdc` to **`.git/info/exclude`, not `.gitignore`**:
`.gitignore` is tracked and reviewed, so appending to it hands the user a diff
they did not ask for, and in a shared repository that diff lands in somebody's
pull request; `info/exclude` is local-only and exactly the mechanism git
provides for "ignore this here, not for everyone." The append is idempotent
(checked line-by-line, not by a whitespace-token membership test, so a
commented-out line doesn't count as already-present) and resolves through
`git rev-parse --git-common-dir` rather than assuming `.git` is a directory -
in a linked worktree or a submodule `.git` is a *file* holding a `gitdir:`
pointer, and `info/exclude` lives under the real common directory that
pointer names, not under the worktree. remem's own development happens inside
a worktree, so this is the ordinary case here, not an edge case. If there is
no repository at all, the `.mdc` is still written and a note (not a warning)
says the exclude was skipped - a workspace outside a repository is an
ordinary thing.

The always-runs half of the hook-contract check follows opencode's shape
exactly: every hook named in the generated `hooks.json` and every key of
`EVENT_KINDS` is checked against the vendored `HOOK_NAMES` -
`tests/test_cursor_install.py` and `tests/test_cursor_event.py`
respectively, both always running - which is what would have caught
subscribing to a hook Cursor does not emit.
`tests/test_cursor_hooks_contract.py` holds the other half: one
`@pytest.mark.cursor` test, which may skip, that re-reads the installed
`Cursor.app` bundle and checks the vendored list is still fresh. Cursor
auto-updates itself, so expect the freshness half to fire eventually, the
same way opencode's did mid-branch.

A Cursor-only install extracts as well as records. `remem events process` used
to be spawned from exactly one place, Claude Code's `SessionStart` hook, so a
Cursor-only or opencode-only setup recorded events forever and never extracted
one. It is now spawned from `hookio.spawn_process`, called both from that hook
and from `remem hook context` - the session-start analogue opencode and Cursor
already call once per session, which makes it the single trigger all three
harnesses share. Fixed once, rather than bolted onto each new install path.

The call sits in a `finally`, so it runs on every path through `hook context`
including the early returns for unusable stdin, an unknown agent and an
unresolvable project: the backlog is global, and whether *this* payload
produced a block says nothing about whether extraction has work waiting.

Extraction shells out to `claude -p`, which a Cursor-only user may well not
have installed. Those jobs **fail and record the reason** rather than being
skipped - `MAX_ATTEMPTS` stops the retries and `remem record status` shows why.
A probe for the extractor was considered and rejected: it can be wrong about
where `claude` lives, while a recorded failure cannot.

Every Cursor hook payload also carries `user_email` (read straight out of
Cursor's payload constructor) and remem stores events in full and
indefinitely, so a recorded Cursor event carries the user's email address as
a side effect of this design - unlike Claude Code's events today. Nothing in
this adapter filters it; the payload is passed through whole, on purpose, for
the same reason every other adapter here does: extraction is the layer meant
to be fixable and re-run without re-recording anything, and pruning fields at
the recording boundary caps what any future extractor could ever see.

**This adapter has recorded real events from a real Cursor session** (2026-08-30,
Cursor 3.18.9). Every payload key `identity()`/`event()` reads (`session_id`,
`workspace_roots`, `hook_event_name`, `tool_name`) was originally derived from
reading Cursor's payload-constructing code in the shipped app bundle, confirmed
by a second independent reading, and has since been confirmed against live
payloads: nine events over two turns, both `EventKind`s, real tool names, and
`workspace_roots` resolving to the right project. The source reading was
correct. See `docs/superpowers/notes/2026-08-29-cursor-payloads.md` for that
reading and `docs/superpowers/notes/2026-08-29-cursor-proof.md` for exactly
what is and is not proven - three of the four proof criteria are closed, and
only one remains open: the block confirmed present in the outbound request,
which cannot be shown from this machine because Cursor's local logs carry no
request bodies. Closing it would take a TLS-intercepting proxy in front of
Cursor, and it is the least valuable of the four now that live payloads have
disproved the failure it stood in for.

Two things the live session taught that are not about Cursor at all. **Cursor
loads Claude Code's `~/.claude/settings.json` hooks and runs them with Cursor
payloads** - so remem's Claude-Code hooks fire inside Cursor, are handed a shape
they cannot parse, and exit 0 in silence. Nothing is broken by it today; it is
undecided territory rather than a bug, and it means the two adapters are not as
independent as the seam suggests. And a `kb.RulesExceedBudget` failure is
**invisible**, because every hook is fail-soft: when the knowledge base outgrew
`REMEM_MAX_CHARS`, context injection silently died on *every* harness, Claude
Code included, with a 0 exit and no output. Fail-soft is still the right
contract, but the budget is the one failure it hides that a user would want to
know about.

## Conventions

- Python 3.14 (`uuid7` from stdlib, `StrEnum`, `from __future__ import annotations`).
- Comments explain *why*, at length, especially where a decision looks arbitrary. Match
  that density; a subtle invariant with no comment reads as an accident to the next reader.
- Prefer failing loudly over quietly doing something else - `UnsupportedScope` is raised
  for `--scope project` rather than falling back.
- In prose and docs: spaced hyphens ` - `, never em dashes.

## Design record

Spec and plan under `docs/superpowers/`; `decisions-2026-08-26*.md` hold the reasoning
behind rulings git history does not capture. Read the relevant decision record before
reversing something that looks odd.
