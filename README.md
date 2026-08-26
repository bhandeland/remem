# remem

A knowledge and memory store for AI coding agents. Agents and humans record
what they learn about a project - facts, reference docs, and prescriptive
rules - and get it back through ranked search or as a context block injected
at session start.

## Requirements

- Python 3.14
- Docker (for Postgres)

## Setup

```bash
docker compose up -d
uv sync
uv run remem db up
```

## Use

```bash
remem remember "Postgres pool sizing" --body "..." --project myapp
remem search "pool sizing"
remem search "pool sizing" --json | jq '.[0].id'
remem kb new myapp --title "myapp knowledge" --project myapp
remem kb show myapp
```

Search is exact by default. When a query matches nothing, remem retries with
typo-tolerant matching and marks those results — `~` in terminal output,
`"fuzzy": true` in `--json` and in the MCP `recall` response. Fuzzy results
appear only when there were no exact ones, so they never dilute a good result
set. `REMEM_FUZZY_THRESHOLD` (default `0.3`) controls how close a match must be.

A knowledge base collects entries two ways: everything matching its query
(`--project` and `--tag`, fixed at creation) plus anything pinned into it with
`remem kb pin <slug> <entry-id>`. A knowledge base created with neither
`--project` nor `--tag` has an empty query and matches nothing until you pin
something.

## Install into Claude Code

```bash
remem install claude-code
```

This registers the MCP server, adds a SessionStart hook that injects the
project's knowledge base, and installs the `remem` skill. Only user scope is
implemented in v1; `--scope project` is rejected rather than quietly ignored.

**The hook injects the knowledge base whose slug is exactly the session
directory's name.** Working in `~/code/myapp` gets you the knowledge base with
slug `myapp`, and nothing else - so create it as:

```bash
cd ~/code/myapp && remem kb new "$(basename "$PWD")" --title "myapp" --project myapp
```

The hook is fail-soft: any problem at all - Postgres down, no matching
knowledge base, migrations missing - means it prints nothing and exits 0. A
knowledge tool must never be why a session will not start. That also means a
silent hook is ambiguous, so set `REMEM_HOOK_DEBUG=1` to have it write the
reason for an empty result to stderr:

```bash
echo '{"cwd":"'"$PWD"'"}' | REMEM_HOOK_DEBUG=1 remem hook session-start
```

## Configuration

Environment variable, then config file, then default.

| setting | env var | default |
|---|---|---|
| Postgres DSN | `REMEM_DSN` | `postgresql://remem:remem@localhost:5433/remem` |
| principal handle | `REMEM_USER_ID` | `brandon` |
| config file | `REMEM_CONFIG` | platform config dir, `remem/config.toml` |
| context budget | `REMEM_MAX_CHARS` | `6000` |
| fuzzy match threshold | `REMEM_FUZZY_THRESHOLD` | `0.3` |
| hook diagnostics | `REMEM_HOOK_DEBUG` | unset (silent) |

## Development

```bash
docker compose up -d
uv run pytest
```

Database tests skip with an explanatory message when Postgres is not running.

Design: `docs/superpowers/specs/2026-08-26-remem-design.md`
