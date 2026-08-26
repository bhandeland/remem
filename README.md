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

## Install into Claude Code

```bash
remem install claude-code
```

This registers the MCP server, adds a SessionStart hook that injects the
project's knowledge base, and installs the `remem` skill. The hook is
fail-soft: if remem is unreachable it prints nothing and exits 0.

## Configuration

Environment variable, then config file, then default.

| setting | env var | default |
|---|---|---|
| Postgres DSN | `REMEM_DSN` | `postgresql://remem:remem@localhost:5433/remem` |
| principal handle | `REMEM_USER_ID` | `brandon` |
| config file | `REMEM_CONFIG` | platform config dir, `remem/config.toml` |
| context budget | `REMEM_MAX_CHARS` | `6000` |

## Development

```bash
docker compose up -d
uv run pytest
```

Database tests skip with an explanatory message when Postgres is not running.

Design: `docs/superpowers/specs/2026-08-26-remem-design.md`
