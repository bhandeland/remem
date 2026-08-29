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
- **`session.open_session()` is the only way to reach the database.** It connects, runs
  migrations, ensures the principal, and hands back a `Session`. Nothing outside
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
(everything fragile is deferred), and `remem events process` - run from cron or a
later SessionStart - extracts entries from sessions that have gone quiet, via
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

`plugin.js` (`src/remem/agents/opencode/plugin.js`) is generated, not written by hand -
`install()` overwrites it unconditionally, no merge, no version marker, no prompt. It
imports nothing, because `$` arrives on `PluginInput`: there is no npm dependency to
install, pin, or keep in step with opencode's own releases. A user who wants local edits
to it is asking for the wrong file - remem owns `remem.js`.

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
