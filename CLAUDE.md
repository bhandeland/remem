# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
docker compose up -d           # Postgres 18 + pgvector on localhost:5433
uv sync
uv run pytest                  # full suite
uv run pytest tests/test_capture_service.py::test_name   # one test
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
services:   services/{write,search,kb,capture}.py    <- every policy decision lives here
store:      store.py (Protocol)  -> backends/postgres/store.py (all SQL)
domain:     domain.py (pure dataclasses/enums, no I/O)
```

- **Frontends parse and format; they never decide.** A rule enforced in a service is one
  every frontend gets for free (query clamping, fuzzy fallback, capture opt-in checks). If
  you find yourself adding a policy branch in `cli.py`, it belongs in `services/`.
- **`store.py` is the portability seam** - a `Protocol`, with Postgres as the only
  implementation. Ownership is enforced *inside* the store (`NotOwner`), not by callers.
- **`session.open_session()` is the only way to reach the database.** It connects, runs
  migrations, ensures the principal, and hands back a `Session`. Nothing outside
  `session.py`/`backends/` should import psycopg. `autocommit=True` is for long-running
  work that records its own progress (the capture drain) - in a single transaction a failed
  statement poisons the connection and the final COMMIT becomes a ROLLBACK.
- **`agents/base.py` + `agents/registry.py` are the pluggability seam** - adapters register
  under the `remem.agents` entry point group and load lazily; a broken third-party adapter
  warns rather than breaking remem.

### Data model

`Entry` (kind: memory/doc/rule, origin: human/agent/capture) is the unit of knowledge.
`Collection` ("knowledge base") membership is two things unioned: a smart `CollectionQuery`
(project + tags + kinds) plus explicitly pinned entries. An empty query matches *nothing*,
forever - `kb.advisories()` exists to say so at creation time.

Search is exact full-text (`tsvector` generated column, weighted title/body/tags), falling
back to trigram similarity **only when exact returns nothing**, never blended. Fuzzy hits
are marked `Hit.fuzzy=True` and every frontend must surface that marker - an agent handed
an unmarked approximate match cites it as certain.

### Migrations

Numbered `.sql` files in `backends/postgres/migrations/`, applied in filename order and
tracked in `schema_migrations`. Add a new numbered file; never edit an applied one.
`migrate()` runs inside the *caller's* transaction - the caller owns commit/rollback.
`applied_versions()` is deliberately read-only (inspecting a DB must not write to it).

Timestamps use `clock_timestamp()`, not `now()`: tests run inside one rolled-back
transaction, where `now()` gives every row an identical `created_at` and makes
`order by created_at desc` non-deterministic.

### Capture

Automatic distillation of finished sessions. Opt-in per project - that gate is the entire
safety story, and it is checked in `services/capture.enqueue`.

Flow: SessionEnd hook does exactly one INSERT into `capture_jobs` (everything fragile is
deferred), then a later SessionStart or `remem capture drain` claims jobs, runs
`claude -p` via `distill/claude_cli.py`, and writes entries with `origin='capture'`.

Invariants worth not breaking:
- Captured entries are **excluded from knowledge base context blocks** (`kb.resolve`
  filters `Origin.CAPTURE`) so machine text never crowds out hand-written rules. They do
  appear in `search`/`recall`. Promote one with `remem kb pin`.
- `distill/base.py` treats all model output as untrusted: shape-checked, capped
  (`MAX_ENTRIES`/`MAX_TITLE`/`MAX_BODY`), filtered before it reaches the store.
- The distillation model is **pinned** (`REMEM_CAPTURE_MODEL`, default `sonnet`), not
  inherited from the session, so cost/behaviour do not drift. Haiku was measured and
  rejected on judgment, not JSON validity.
- `CHILD_ENV_VAR` (`REMEM_CAPTURE_CHILD`) is set on the spawned `claude -p` so its own
  hooks refuse to recurse. Both hooks check it. Do not rename it on one side only.
- Jobs stop retrying after `MAX_ATTEMPTS`; `remem capture drain --job ID` retries by id.
  Failures record the reason *and* the model's raw output, both separately truncated.

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
