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
uv tool install --editable .    # puts `remem` on your PATH
remem db up
```

`uv tool install` is not optional if you intend to use the Claude Code
integration. The MCP server and both hooks are registered as bare `remem`, so a
`remem` that exists only inside the project venv produces a configuration that
silently does nothing: the hooks fail soft to silence and the MCP server never
starts. `--editable` means changes to this repo take effect without reinstalling.

## Use

```bash
remem rule "Spaced hyphens, never em dashes"  --body "..."
remem remember "Postgres pool sizing" --body "..."
remem remember "A longer note" --edit          # compose the body in $EDITOR
remem search "pool sizing"
remem search "pool sizing" --json | jq '.[0].id'
remem kb new myapp --title "myapp knowledge" --project myapp
remem kb show myapp
```

`remember` and `rule` default `--project` to the repository's name - the same
name the knowledge base injected at session start queries on. It comes from git
rather than the directory, so a subdirectory or a worktree still files under the
project it belongs to (`cd src` used to file entries under a project called
`src`, where nothing would look for them). Pass
`--global` for knowledge that is not tied to one project. (Before this
defaulted, forgetting `--project` stored an entry with no project: the write
succeeded and the entry simply never appeared in the knowledge base.)

`rule` is shorthand for `remember --kind rule`. Rules are injected into every
session and never truncated, so they are the ones worth writing down.

Search is exact by default. When a query matches nothing, remem retries with
typo-tolerant matching and marks those results — `~` in terminal output,
`"fuzzy": true` in `--json` and in the MCP `recall` response. Fuzzy results
appear only when there were no exact ones, so they never dilute a good result
set. `REMEM_FUZZY_THRESHOLD` (default `0.3`) controls how close a match must be.

## Automatic capture

remem can distil finished sessions into entries by itself. It is **off** until
you turn it on for a project:

```bash
remem capture enable          # this directory
remem capture status
```

When a session ends, a hook records it in a queue. Distillation happens later —
on your next session, or when you run `remem capture drain` — by asking
`claude -p` to extract at most five durable facts. Captured entries have
`origin='capture'`, appear in `remem search` and the MCP `recall` tool, and are
deliberately **excluded from knowledge base context blocks** so machine-written
text never crowds out rules you wrote. Promote a good one with `remem kb pin`.

Nothing is captured from a project you have not enabled.

Distillation runs `claude -p` with the model pinned by `REMEM_CAPTURE_MODEL`
(default `sonnet`), roughly $0.10-0.25 per session. It is pinned rather than
inherited so cost and behaviour do not change when you switch your own session
model. Measured on a real 40KB transcript, `haiku` cost about a third as much
but returned one usable entry out of three - a platitude, and an open question
recorded as a durable rule - where `sonnet` and `opus` each returned two out of
two. Deciding what will still be true in a month is a judgement task, not a
compression one, so the cheap tier costs more than it saves.

`remem capture status` lists failures with the reason recorded against each
job, including the model's own output when it returned something that could
not be read as entries. A job that has failed too many times stops being
retried automatically; retry it by id with `remem capture drain --job ID`.

A knowledge base collects entries two ways: everything matching its query
(`--project` and `--tag`) plus anything pinned into it with
`remem kb pin <slug> <entry-id>`. A knowledge base created with neither
`--project` nor `--tag` has an empty query and matches nothing until you pin
something — `remem kb new` warns when it creates one.

Change a query later with `remem kb query <slug> --tag X --project Y`, or
`--clear` it so the knowledge base holds only what you pinned. Pinned entries
are never affected.

## Session handoff

A long session is expensive to keep going and cheap to hand off. remem makes
the boundary deliberate:

```bash
remem handoff write --topic gitlab-ci --body "..."   # or --edit
remem handoff latest --topic gitlab-ci
remem search "runner cache" --handoff
```

A handoff is an ordinary entry with `origin='handoff'`, a `topic:<slug>` tag,
and four sections: Done, In flight, Next steps, Gotchas. Writing one supersedes
the previous handoff for the same topic, so a project accumulates dozens of
them and still has exactly one live entry per workstream.

Handoffs are excluded from knowledge base context blocks and from search unless
you pass `--handoff`. The next session in that project sees a one-line pointer
instead:

    Handoff available: gitlab-ci (2h ago) - run remem-prime gitlab-ci

The `remem-handoff` and `remem-prime` skills drive the cycle:
handoff, `/clear`, prime. A `UserPromptSubmit` hook counts the session's turns
and suggests a handoff at 150, then every 50 after that - `REMEM_TURN_WARN_AT`
and `REMEM_TURN_WARN_EVERY` tune it, and the warning always asks for a handoff
at the next task boundary rather than mid-task.

## Install into Claude Code

```bash
remem install claude-code
```

This registers the MCP server, adds SessionStart and SessionEnd hooks, and
installs the `remem` skill. Both config files are backed up before they are
written. Only user scope is implemented in v1; `--scope project` is rejected
rather than quietly ignored.

Check `which remem` first. Everything registered here calls bare `remem`, so
without it on your PATH the install appears to succeed and then does nothing at
all - the hooks are fail-soft and say nothing, and the MCP server simply never
starts. See Setup.

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
| distillation model | `REMEM_CAPTURE_MODEL` | `sonnet` |
| hook diagnostics | `REMEM_HOOK_DEBUG` | unset (silent) |
| session-size warning | `REMEM_TURN_WARN_AT` | `150` |
| warning interval | `REMEM_TURN_WARN_EVERY` | `50` |

## Development

```bash
docker compose up -d
uv run pytest
```

Database tests skip with an explanatory message when Postgres is not running.

Design: `docs/superpowers/specs/2026-08-26-remem-design.md`
