# remem

> **This repo is mirrored from [GitLab](https://gitlab.com/nighthawk-oss/remem).** Issues, merge requests, and contributions should go there.

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

Search has three tiers, tried in order and never blended: exact full-text,
then semantic (related meaning, different words), then typo-tolerant trigram
matching. Each runs only when the one above returned nothing, so approximate
results never dilute a good result set. Every result says which tier found it -
unmarked, `~`, or `?` in terminal output, and `"match": "exact" | "semantic" |
"fuzzy"` in `--json` and in the MCP `recall` response. (There was no `match`
before the semantic tier; the key it replaced, `"fuzzy": true`, is gone rather
than aliased.) `REMEM_FUZZY_THRESHOLD` (default `0.3`) and
`REMEM_SEMANTIC_THRESHOLD` (default `0.55`) control how close a match must be
in the tier each names.

The semantic tier needs two things you have to opt into, and does nothing
without both:

```bash
uv tool install --editable '.[embed]'   # the local embedding model
remem embed                             # embed entries that have no vector
```

The `[embed]` extra installs a local ONNX embedder - no API key, no per-call
cost, and the first run downloads about 130MB of model weights. `remem embed`
is idempotent and only does what is missing, so re-run it after writing entries
or from cron; entries written since the last run have no vector and are
invisible to the semantic tier until it is. Without the extra, search is the
two tiers it has always been - exact, then trigram - and it says nothing about
it, because a missing optional dependency is not an error at search time.
Changing `REMEM_EMBED_MODEL` makes every existing vector stale and means
re-running `remem embed`.

## Events and extraction

remem can turn finished sessions into entries by itself, from raw events it
records as you work. It is **off** until you turn it on for a project:

```bash
remem record enable          # this directory
remem record status
```

```
harness hook / plugin   ->  remem record event      one INSERT, fail-soft
cron (or SessionStart)  ->  remem events process    extract -> entries + provenance
cron                    ->  remem embed              entries lacking a current vector
on request              ->  remem events prune       extracted events, explicit window
```

A hook records one event per tool call - the raw payload, in full, kept
**indefinitely**. Nothing prunes on a schedule; `remem events prune --before 30d`
is the only thing that ever deletes one, and only when you ask. Once a session
has gone quiet for `REMEM_IDLE_MINUTES` (default 20), `remem events process`
asks `claude -p` to extract at most five durable facts from its events.
Extracted entries have `origin='extracted'`, appear in `remem search` and the
MCP `recall` tool, and are deliberately **excluded from knowledge base context
blocks** so machine-written text never crowds out rules you wrote. Promote a
good one with `remem kb pin`.

Nothing is recorded from a project you have not enabled.

Extraction runs `claude -p` with the model pinned by `REMEM_EXTRACT_MODEL`
(default `sonnet`), roughly $0.10-0.25 per session. It is pinned rather than
inherited so cost and behaviour do not change when you switch your own session
model. Measured on a real 40KB transcript, `haiku` cost about a third as much
but returned one usable entry out of three - a platitude, and an open question
recorded as a durable rule - where `sonnet` and `opus` each returned two out of
two. Deciding what will still be true in a month is a judgement task, not a
compression one, so the cheap tier costs more than it saves.

`remem record status` lists failures with the reason recorded against each
job, including the model's own output when it returned something that could
not be read as entries. A job that has failed too many times stops being
retried automatically; retry it by id with `remem events process --job ID`.

A knowledge base collects entries two ways: everything matching its query
(`--project` and `--tag`) plus anything pinned into it with
`remem kb pin <slug> <entry-id>`. A knowledge base created with neither
`--project` nor `--tag` has an empty query and matches nothing until you pin
something - `remem kb new` warns when it creates one.

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
repository's name.** Working anywhere in `~/code/myapp` - including a
subdirectory or a git worktree of it - gets you the knowledge base with slug
`myapp`, and nothing else. Create it as:

```bash
cd ~/code/myapp && remem kb new myapp --title "myapp" --project myapp
```

The hook is fail-soft: any problem at all - Postgres down, no matching
knowledge base, migrations missing - means it prints nothing and exits 0. A
knowledge tool must never be why a session will not start. That also means a
silent hook is ambiguous, so set `REMEM_HOOK_DEBUG=1` to have it write the
reason for an empty result to stderr:

```bash
echo '{"cwd":"'"$PWD"'"}' | REMEM_HOOK_DEBUG=1 remem hook session-start
```

## Install into opencode

```bash
remem install opencode --scope project   # writes .opencode/plugin/remem.js
remem install opencode --scope user      # writes ~/.config/opencode/plugin/remem.js
```

Unlike the Claude Code adapter, this one writes a single generated file - no
JSON to merge, no MCP registration, no skill. `remem install opencode` (and
`verify`) overwrite `remem.js` unconditionally; it is not a file to hand-edit.

**Recording is off until you enable it per project**, exactly as above:

```bash
remem record enable --project myapp
```

An install that leaves you waiting for events that never arrive is the
failure this whole design cares most about - opencode's own hooks run and
call `remem record event` regardless, but nothing lands in the database for a
project you have not opted in.

**The knowledge base block is injected once per session, not once per
turn.** opencode has no session-start hook - `experimental.chat.system.transform`
fires on every message instead - so the plugin keeps an in-process set of
session ids it has already injected for and skips every message after the
first. The consequence: a memory you write mid-session is not visible to that
session. It shows up starting with the next one.

## Configuration

Environment variable, then config file, then default.

| setting | env var | default |
|---|---|---|
| Postgres DSN | `REMEM_DSN` | `postgresql://remem:remem@localhost:5433/remem` |
| principal handle | `REMEM_USER_ID` | `brandon` |
| config file | `REMEM_CONFIG` | platform config dir, `remem/config.toml` |
| context budget | `REMEM_MAX_CHARS` | `6000` |
| fuzzy match threshold | `REMEM_FUZZY_THRESHOLD` | `0.3` |
| semantic match threshold | `REMEM_SEMANTIC_THRESHOLD` | `0.55` |
| embedding model | `REMEM_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` |
| extraction model | `REMEM_EXTRACT_MODEL` | `sonnet` |
| extraction idle wait | `REMEM_IDLE_MINUTES` | `20` |
| hook diagnostics | `REMEM_HOOK_DEBUG` | unset (silent) |
| session-size warning | `REMEM_TURN_WARN_AT` | `150` |
| warning interval | `REMEM_TURN_WARN_EVERY` | `50` |

## Development

```bash
docker compose up -d
uv run pytest
```

Database tests skip with an explanatory message when Postgres is not running.

Design: `docs/superpowers/specs/2026-08-26-remem-design.md`,
`docs/superpowers/specs/2026-08-28-events-and-recall-design.md`
