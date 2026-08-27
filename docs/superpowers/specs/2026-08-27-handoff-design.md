# remem session handoff - design

Date: 2026-08-27
Status: approved, ready for implementation planning
Builds on: docs/superpowers/specs/2026-08-26-remem-design.md,
docs/superpowers/specs/2026-08-26-capture-design.md

## Purpose

Make `/clear` cheap. A long session accumulates state that exists nowhere but
its own context: what landed, what is still running, what to do next, and the
constraints discovered along the way. Today the only way to keep that is to not
clear, so sessions run long and expensive.

This design adds three things that work as one loop:

1. A **size warning** that notices when a session has grown past a threshold
   and says so, so the boundary is chosen rather than hit.
2. A **handoff** skill that writes the session's state into remem as one entry.
3. A **prime** skill that rehydrates a fresh session from that entry, without
   exploring the repository.

`/remem-handoff` -> `/clear` -> `/remem-prime <topic>` is the intended cycle.

## Requirements

1. A handoff is the complete resume payload: a fresh session needs nothing else
   to continue the work.
2. Handoffs never appear in knowledge base context blocks, and never in default
   search results. Dozens of them must cost nothing to the rest of remem.
3. Only the newest handoff per topic is live. Older ones stay reachable.
4. The size warning runs on every user prompt and must be cheap and fail-soft.
5. Priming reads stored knowledge only. No repository exploration.
6. No new storage system: a handoff is an entry.

## Non-goals

- Obsidian journal entries, wikilinks, or a knowledge corpus rebuild. Those
  belong to the user's vault tooling, not to remem.
- Auto-injecting handoff bodies into every session. A one-line pointer only.
- Automatic handoff writing at SessionEnd. Capture already distils transcripts;
  a handoff is a deliberate act with the user in the room.
- Cross-project handoffs. A handoff belongs to one project.
- Compaction control. remem suggests clearing; it does not clear.

## Data model

A handoff is an ordinary `Entry`. No new table, no new columns.

| field | value |
|---|---|
| `kind` | `doc` |
| `origin` | `handoff` (new) |
| `project` | resolved from git, as every other write |
| `tags` | `["topic:<slug>"]` |
| `title` | `Handoff: <slug> (<YYYY-MM-DD>)` |
| `body` | the four sections, below |

`origin` is a Postgres enum, so the new value needs a migration:

```sql
-- 005_handoff.sql
alter type entry_origin add value if not exists 'handoff';
```

`migrate()` runs the batch inside the caller's transaction. Since PG 12 that is
allowed for `add value`, with one restriction that matters here: the new value
cannot be *used* in the same transaction that adds it. Nothing in the migration
inserts a handoff row, so the constraint is satisfied - but a later migration
that both adds an enum value and writes it would fail, and the file says so.

`if not exists` makes the statement idempotent, which matters because a
database migrated by an older build could already carry the value.

### The body

Exactly four sections, in this order, always present even when empty:

```markdown
## Done
## In flight
## Next steps
## Gotchas
```

Done and In flight carry URLs and ids as plain text, not markdown links. Next
steps are numbered and in priority order. Gotchas are the non-obvious
constraints found this session that are not yet stored as memories or rules.

The format is fixed rather than free-form so prime knows what it is reading and
the user can scan a handoff without reading it.

### One live handoff per topic

Writing a handoff for a `(project, topic)` that already has a live one
supersedes the old entry through the existing `store.set_superseded`. The
supersede chain is the history: every handoff ever written is still there, and
`remem get <id>` still reads it, but only the newest is live.

This is what makes the volume tolerable. Search, resolution, and the partial
indexes in 002 all filter `superseded_by is null`, so a topic handed off fifty
times costs the same as one handed off once.

## Keeping handoffs out of the way

Two exclusions, at two layers.

**Context blocks.** `kb.resolve` already filters to `origins=[HUMAN, AGENT]`.
Handoffs are excluded by that existing line with no change.

**Search.** `Query.origins` means "only these; empty means all", so handoffs
would show up in every search by default. The exclusion belongs in the service
where both frontends inherit it:

```python
def find(store, owner_id, query, fuzzy_threshold=..., include_handoffs=False):
    if not include_handoffs and not query.origins:
        query = replace(query, origins=[Origin.HUMAN, Origin.AGENT, Origin.CAPTURE])
```

An explicit `origins` from the caller is never overridden - asking for
`origins=[HANDOFF]` means asking for handoffs. `remem search --handoff` and the
MCP `recall(include_handoffs=true)` set the flag.

Note the enum-completeness hazard: this list must gain any future origin, or a
new origin silently vanishes from search. The alternative - an `exclude_origins`
field on `Query` - avoids that but adds a second, overlapping filter to the
store's SQL for one caller. Taken deliberately, with a comment on the list.

## The size warning

A `UserPromptSubmit` hook, `remem hook session-size`.

```
payload {transcript_path, session_id}
  -> count turns
  -> read last-warned for this session
  -> if count >= at and count - last >= every: print reminder, record count
  -> exit 0
```

**Counting turns.** Lines in the transcript JSONL whose `type` is `user`. The
file is read as a stream and matched per line rather than parsed as JSON: the
transcript reaches megabytes in exactly the sessions this hook exists for, and
the count only needs to be approximately right to decide when to warn.

**Thresholds.** `REMEM_TURN_WARN_AT` (default 150) and `REMEM_TURN_WARN_EVERY`
(default 50), resolved through `config.load()` like every other setting. Both
fall back to the default when unparseable or non-positive, as `max_chars` does.

**State.** A JSON file under `user_cache_path("remem")`, keyed by session id,
holding the count at which this session was last warned. A stateless `count %
every == 0` test is tempting and wrong: the count does not always advance by
exactly one per prompt, and a skipped remainder means the warning never fires
again for that session. Comparing against the last warned count fires on the
first prompt at or past each step regardless of stride.

The file is best-effort in both directions. Unreadable or corrupt reads as
"never warned"; a failed write means the next prompt warns again. Neither can
break a session, and both fail toward warning rather than toward silence.

Entries are keyed by session id and pruned when older than 7 days, so the file
does not grow forever. Pruning happens on write, not on a schedule.

**Output.** The reminder goes to stdout, which Claude Code injects as context
for that prompt. It names the count and asks for a handoff *at the next task
boundary* - a warning that interrupts work in progress is a warning the user
learns to ignore.

**Contract.** Identical to the existing hooks: bounded work, exit 0
unconditionally, print nothing on any error, reasons to stderr only under
`REMEM_HOOK_DEBUG`. This one runs on every single prompt, so it is also the
hook with the least excuse for touching Postgres - and it does not.

## The resume pointer

`hook session-start` gains one line when a live handoff exists for the
project:

```
Handoff available: gitlab-ci (2h ago) - run /remem-prime gitlab-ci
```

Two structural points. The pointer is appended *after* `kb.render`, outside the
character budget - it is a fixed ~20 tokens and making it compete with rules
would be absurd. And the pointer must be emitted even when the project has no
knowledge base at all, so `session_start` becomes: build the block, build the
pointer, join whichever are non-empty, return `""` only when both are.

The existing fail-soft guarantee covers the new lookup: any failure reading the
handoff yields no pointer, never a broken session.

## Interfaces

### CLI

```bash
remem handoff write --topic gitlab-ci --body "..."   # or --edit, or - for stdin
remem handoff latest [--topic X] [--json]
remem search "pipeline" --handoff
remem hook session-size                              # not for interactive use
```

`--topic` defaults to the project name, so a single-workstream repository never
has to think about topics. `--project` behaves as it does on `remember`, but
there is no `--global`: a handoff with no project is rejected rather than
stored, since nothing - not the pointer, not `handoff latest` - could ever
resolve it.

`handoff latest` prints the entry whole. With no live handoff it prints a short
"none for this project" and exits 0 - a missing handoff is an ordinary state,
not an error.

There is no `handoff list`. `remem search --handoff` covers it, and the
supersede chain means the interesting answer is nearly always "the live one".

### MCP

No new tools. `recall` gains `include_handoffs: bool = False`. The skills drive
handoff writing through the CLI, which already has the editor and stdin paths.

### Skills

The bundled skill directory is restructured:

```
skill/SKILL.md                   ->  skills/remem/SKILL.md
                                     skills/remem-handoff/SKILL.md
                                     skills/remem-prime/SKILL.md
```

The adapter installs every subdirectory of `skills/` rather than one hardcoded
file, so a fourth skill later is a new directory and nothing else.
`pyproject.toml`'s force-include follows the rename.

**remem-handoff** - triggers on wrap-up language, the size warning, or a
proposed `/clear`. Steps: pick the topic slug (default the project, ask only if
genuinely ambiguous) -> write the four sections through `remem handoff write`
-> fold anything durable into `remem remember` / `remem rule`, since a gotcha
that will still be true next month belongs in the store rather than in a
handoff that gets superseded -> print the entry id and the exact resume
command. Constraint: no new work starts during a handoff; if the user asks for
one more thing, the handoff happens after it.

**remem-prime** - triggers on `/remem-prime <topic>` or "pick up where we left
off". Steps: `remem handoff latest --topic X` -> `remem search` on the topic
for anything written after it -> a brief covering state, next steps, and
gotchas, then ask which step to start. Constraints: no repository exploration
during priming - main-thread exploration output is re-sent every turn for the
rest of the session, which is the cost this whole feature exists to avoid; on
conflict the newest source wins and the conflict is named in one line; with no
handoff and no search hits, say so and ask for a pointer rather than going
looking.

### Installer

`remem install claude-code` additionally registers the `UserPromptSubmit` hook
(`remem hook session-size`, timeout 5) using the same
already-registered check as the other two, and installs the three skills. The
report gains a note naming the warning thresholds and the handoff cycle.

## Failure handling

| failure | behaviour |
|---|---|
| transcript missing or unreadable | no warning, exit 0 |
| cache file corrupt | treated as never warned; warns this prompt |
| cache write fails | warning repeats next prompt |
| Postgres down during handoff write | CLI reports it and exits non-zero - a handoff that silently did not save is the one failure this feature cannot have |
| Postgres down at SessionStart | no pointer, no block, exit 0 |
| no live handoff at prime time | prime says so and asks for a pointer |

The asymmetry is deliberate. Hooks are fail-soft because a session must start;
`remem handoff write` is fail-loud because the user is standing there about to
throw the context away.

## Testing

No test spawns a session or an LLM.

**Pure, no database:** turn counting against fixture JSONL (including
malformed lines, an empty file, and a file with no user turns); the threshold
rule across strides of 1 and 7, at exactly `at`, and past several steps; cache
read/write round-trip, corrupt file, and pruning; the pointer line's format and
its "no knowledge base but a handoff exists" path.

**`db`-marked:** writing a handoff supersedes the prior one for the same
`(project, topic)` and leaves a different topic's alone; a superseded handoff
does not resurface in `handoff latest`; handoffs are absent from `search` by
default and present with `--handoff`; handoffs are absent from `kb.resolve`;
`handoff write` with no resolvable project is rejected.

**Installer:** the `UserPromptSubmit` hook is registered exactly once across
two installs, and all three skills land in `~/.claude/skills/`.

**Migration:** `005` applies to a database already at `004`, and the new origin
round-trips through `put_entry`/`get_entry`.

## Files

| file | change |
|---|---|
| `domain.py` | `Origin.HANDOFF` |
| `backends/postgres/migrations/005_handoff.sql` | new |
| `services/handoff.py` | new - write, latest, topic slugging, supersede rule |
| `services/search.py` | `include_handoffs` |
| `services/kb.py` | unchanged (already filters origins) |
| `session_size.py` | new - turn counting, threshold rule, warn state |
| `agents/claude_code/hook.py` | `session_size()`, pointer in `session_start()` |
| `agents/claude_code/adapter.py` | third hook, skills directory install |
| `cli.py` | `handoff` sub-app, `hook session-size`, `search --handoff` |
| `mcp_server.py` | `recall(include_handoffs=...)` |
| `agents/claude_code/skills/` | restructure + two new skills |
| `pyproject.toml` | force-include follows the rename |
| `README.md`, `CLAUDE.md` | the cycle, the new settings |

`session_size.py` sits at the top level rather than under `agents/claude_code/`
because counting turns in a transcript is not Claude-Code-specific policy; the
hook that calls it is.

## Deferred

- Handoffs for agents other than Claude Code.
- A `handoff diff` showing what changed between two handoffs of a topic.
- Pruning superseded handoffs. The chain is small and the indexes ignore it;
  revisit if a project ever accumulates thousands.
- Teaching capture to write a handoff when a session ends without one.
