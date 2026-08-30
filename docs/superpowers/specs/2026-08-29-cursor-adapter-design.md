# Cursor adapter - design

Date: 2026-08-29
Status: implemented (2026-08-29) - shipped unproven against a live Cursor
session; see `docs/superpowers/notes/2026-08-29-cursor-proof.md` for exactly
what is and is not proven.
Builds on: docs/superpowers/specs/2026-08-28-events-and-recall-design.md
Follows: docs/superpowers/specs/2026-08-29-opencode-adapter-design.md

## Purpose

The events-and-recall design names three harnesses - Claude Code, Cursor,
opencode - and has now shipped two. This design covers the third and last.

It is the smallest of the three adapters and the one that needs the most
correcting, because the events-and-recall spec's description of Cursor is
wrong. That spec built its harness table from claude-mem's
`cursor-hooks/PARITY.md`, a second-hand map of a much older Cursor, and every
design decision that leaned on the Cursor column leaned on stale facts. The
first job of this document is to replace them with facts read out of the
shipped product.

## What Cursor actually gives us

Verified against Cursor.app **3.9.16**, by reading the hook enumeration and the
Claude Code compatibility table out of
`/Applications/Cursor.app/Contents/Resources/app/out/vs/workbench/workbench.desktop.main.js`.
Configuration is a `hooks.json` file, at `~/.cursor/hooks.json` for user scope
and `<repo>/.cursor/hooks.json` for project scope, whose entries name a command
that receives its payload as JSON on stdin.

Cursor 3.9.16 emits **21** hooks:

```
beforeShellExecution  beforeMCPExecution   afterShellExecution  afterMCPExecution
beforeReadFile        afterFileEdit        beforeTabFileRead    afterTabFileEdit
stop                  beforeSubmitPrompt   afterAgentResponse   afterAgentThought
sessionStart          sessionEnd           preCompact           subagentStart
subagentStop          preToolUse           postToolUse          postToolUseFailure
workspaceOpen
```

Six of them block - Cursor waits on the hook and reads a permission decision
from its stdout: `beforeShellExecution`, `beforeMCPExecution`, `beforeReadFile`,
`beforeTabFileRead`, `subagentStart`, `preToolUse`. **This design subscribes to
none of them.** Every hook it uses is one Cursor does not wait on for an answer,
which is what keeps remem's fail-soft contract meaningful here: a hook that
cannot deny anything cannot deny anything by failing.

Cursor also ships a Claude Code compatibility layer, which accepts Claude Code's
hook names and tool names and maps them onto its own:

| Claude Code | Cursor |
|---|---|
| `SessionStart` | `sessionStart` |
| `PostToolUse` | `postToolUse` |
| `UserPromptSubmit` | `beforeSubmitPrompt` |
| `Stop` / `SubagentStop` | `stop` / `subagentStop` |
| `SessionEnd` / `PreCompact` | `sessionEnd` / `preCompact` |
| `Bash` / `Read` / `Write` / `Edit` | `Shell` / `Read` / `Write` / `Write` |

`PermissionRequest` and `Notification` map to nothing, and `Glob` is rejected
outright with a warning. **This design does not use the compatibility layer.**
It writes Cursor's own names. Compatibility layers are a migration aid for
configs written against another product; an adapter written for Cursor should
say what it means in Cursor's vocabulary, and the layer is one more thing that
could be removed under us.

### Three corrections to the events-and-recall spec

The load-bearing sentences, and what is actually true:

1. *"Cursor: tool events only, **no transcript**"* - false. `beforeSubmitPrompt`
   carries the user's prompt and `afterAgentResponse` the assistant's reply.
   Cursor's raw material is richer than opencode's, not poorer.

   **Correction, 2026-08-29 (Task 7, after this document was written).** The
   sentence directly above - "There is no transcript file, but the conversation
   is available as message events" - was itself wrong, and for the same
   root cause as the mistake it was correcting: it assumed Cursor's shape from
   another tool's map instead of reading Cursor's own code all the way through.
   Cursor's hook payload constructor puts `transcript_path` on every hook
   envelope except `workspaceOpen` - see
   `docs/superpowers/notes/2026-08-29-cursor-payloads.md`. There **is** a
   transcript file, on every hook this adapter subscribes to. This does not
   change anything this design built: nothing here reads `transcript_path`,
   and using the transcript remains out of scope for this branch - it is
   recorded here rather than acted on, per `docs/superpowers/notes/2026-08-29-cursor-proof.md`.

2. *"Neither Cursor nor opencode has a reliable session-end signal"* - false for
   Cursor, which has both `sessionStart` and `sessionEnd`.

3. *"no SessionStart equivalent in Cursor"*, inherited from PARITY.md - false,
   and it was the premise behind claude-mem rewriting its rules file on every
   single prompt.

One claim in that spec **survives**, and it is the one that matters most:
context cannot be injected through stdout. Cursor's `hookSpecificOutput`
compatibility is scoped to `PreToolUse` permission decisions only -
`permissionDecision`, `permissionDecisionReason`, `updatedInput` - and there is
no `additionalContext` path. Writing `.cursor/rules/*.mdc` remains the only way
in.

The events-and-recall spec is amended by this document rather than rewritten.
The correction is dated and the reason recorded: it inherited a competing
tool's map instead of reading the territory. That is the reusable lesson, and
deleting the wrong table would delete it too.

### What `sessionEnd` does not change

Extraction stays triggered by idleness, not by `sessionEnd`. The reasoning in
the events-and-recall spec was stated as "two of three harnesses have no end
hook"; the count is now one of three, and the conclusion is unchanged, because
the argument never actually rested on the count:

- opencode still has none, so a session-end trigger would still not be the
  mechanism all three harnesses share.
- A hook that exists is not a hook that always fires. A crash, a force quit or
  a machine sleeping strands a session forever under an end-hook trigger, and
  ends it normally under a clock. This is the same reasoning the opencode design
  recorded for deliberately not using `experimental.session.compacting`.

`sessionEnd` is therefore left unsubscribed, and this paragraph is why.

## Requirements

1. Record tool calls and both sides of the conversation from Cursor, through the
   same `remem record event` every other adapter uses.
2. Inject the knowledge base block into Cursor sessions, once per session.
3. Never write to a hook Cursor waits on for a decision.
4. Merge into `hooks.json` rather than owning it - other tools write there too.
5. Never leave a surprise in the user's git status or in a tracked file.
6. Subscribing to a hook Cursor does not emit must fail a test that runs on CI.

## Design

### What ships, and where

`src/remem/agents/cursor/`, four modules, and **no generated script of any
kind** - not a plugin file, not a shell script. `hooks.json` names the `remem`
command directly, which is available because `remem record event` already
reads its payload as JSON on stdin.

| module | holds |
|---|---|
| `hooks.py` | the vendored 21 hook names, `CURSOR_VERSION`, the freshness reader |
| `install.py` | `hooks_path(scope, home, cwd)`; the `hooks.json` merge |
| `rules.py` | rendering `.cursor/rules/remem.mdc`; the `.git/info/exclude` entry |
| `adapter.py` | `identity` / `event` / `install` / `verify` / `inject` |

This adapter sits between the two that shipped, and deliberately borrows from
each. opencode's `remem.js` is machine-owned and overwritten unconditionally,
because remem is the only thing that ever writes it. `.cursor/hooks.json` is
**user-owned and shared** - another tool's hooks can legitimately be in there -
so it gets Claude Code's treatment instead: read, merge, back the file up first,
and echo the backup path. A `.bak<timestamp>` nobody is told about is barely a
safety net, per the settings work.

The generated `.mdc`, by contrast, **is** machine-owned and overwritten
unconditionally, like `remem.js`. A user who wants local edits to it is asking
for the wrong file.

### Scope

Both, as opencode does:

- `user` - `~/.cursor/hooks.json`
- `project` - `<repo>/.cursor/hooks.json`
- anything else raises `UnsupportedScope`, never a quiet fallback.

User scope is not crippled by having only one `hooks.json`, because the rules
file is resolved from the workspace root in the payload at hook time, not from
anything baked in at install time. One user-scope install therefore injects
correctly into every repository the user opens.

### Injection becomes an adapter capability

Cursor needs the block written to a file; Claude Code prints it to stdout;
opencode returns it from a plugin transform. Rather than teach `cli.py` what an
`.mdc` is, injection becomes the fourth **optional adapter capability**, probed
with `getattr` exactly as `event()`, `env_settings()` and `settings_path()` are:

```python
def inject(self, block: str, payload: dict) -> str | None: ...
```

- Claude Code does not implement it. `remem hook context --agent claude-code`
  prints, as it does today; stdout is the frontend's job there.
- Cursor implements it: write the `.mdc`, return the path written, so the CLI
  can report where the block went without knowing what it wrote.
- Same degradation contract as every other probed capability: an adapter that
  raises warns and continues, and a broken third-party adapter is never why
  `remem hook context` will not run.

This is not a new seam. The events-and-recall spec already specified it -
"Context injection differing per harness is why it is an adapter capability,
probed with `getattr` and documented on the Protocol" - and the opencode work
built `services/context.py` so that the block could be produced independently of
how it is delivered. This design is the first caller that needs the delivery
half, and it cashes a cheque already written.

### Hook wiring

Four entries, four hooks:

| hook | command | becomes |
|---|---|---|
| `sessionStart` | `remem hook context --agent cursor` | writes the `.mdc` |
| `postToolUse` | `remem record event --agent cursor` | `EventKind.TOOL_CALL` |
| `beforeSubmitPrompt` | `remem record event --agent cursor` | `EventKind.MESSAGE` |
| `afterAgentResponse` | `remem record event --agent cursor` | `EventKind.MESSAGE` |

`sessionStart` is absent from the adapter's `EVENT_KINDS` table and so can never
reach `event()` - it injects rather than records, exactly as
`experimental.chat.system.transform` is absent from opencode's table for the
same reason.

Three deliberate omissions:

- **`afterAgentThought`.** Reasoning text is high-volume and low-signal for
  extraction, and recording it doubles event count for one harness only.
- **`afterShellExecution` / `afterMCPExecution` / `afterFileEdit`**, in favour of
  the generic `postToolUse`. One parser instead of three, and no gap opens when
  Cursor adds a tool type. The specific hooks remain in the vendored list, so
  choosing them later is an edit to one table, not a redesign.
- **`beforeReadFile`.** One event per file read, and it blocks.

### Once per session, by construction

`sessionStart` fires once, so the `.mdc` is written once, and no state has to be
kept anywhere to make that true.

This is worth stating because it is the second time this project has reached for
per-session state and the second time the answer differed. Claude Code gets
once-per-session free from `SessionStart`. opencode has no session hook at all,
so its plugin keeps a module-level `Set` of session ids - the only policy in
that JavaScript, and only because per-process state had nowhere else to live.
Cursor was expected to need something worse than opencode's `Set`, since a shell
hook has no process to keep state in and would have needed a marker file in the
platform cache directory. It needs neither. The correction to the stale spec is
what removed that whole mechanism.

### The rules file, and the user's repository

`.cursor/rules/remem.mdc`, written into the workspace root from the hook
payload, with `alwaysApply: true` frontmatter so Cursor includes it in every
chat.

This is the first time remem writes context **into the user's working tree**
rather than into a stream, and that gets a policy rather than a shrug:

- The path is added to **`.git/info/exclude`**, not `.gitignore`. `.gitignore`
  is tracked, reviewed and merged; appending to it hands the user a diff they
  did not ask for, and in a shared repository that lands in somebody's pull
  request. `.git/info/exclude` is local-only, needs no commit, and is exactly
  the mechanism git provides for "ignore this here, not for everyone".
- Committing the `.mdc` is the outcome to prevent, not merely to discourage. Its
  content is one user's knowledge base rendered at one moment: publishing it
  publishes personal notes, and it is stale the moment the knowledge base
  changes.
- Adding the exclude entry is idempotent and never rewrites the file - append
  the line if absent, do nothing if present.
- If there is no `.git` directory the `.mdc` is still written and a note says
  the exclude was skipped. Not a warning: a workspace outside a repository is
  an ordinary thing, not a failure.

### The Python side

`identity()` and `event()` follow the opencode adapter closely: the payload is
passed through **whole** and never pruned, because extraction is the half of the
pipeline meant to be fixable and re-runnable without re-recording anything, and
an adapter that dropped fields here would cap what any future extractor could
ever see. A payload with no session id returns `None` - without one the event
cannot be grouped, and idle-triggered extraction is per session.

`install()` merges `hooks.json`, writes nothing else, and finishes with
`verify()`, which is `agents.verify.round_trip` unchanged - record an event, read
it back, delete it, against the real database. An install that cannot
demonstrate recording says so. This is why the install tests are marked `db`.

**One thing this document cannot specify: Cursor's payload field names.** Which
key carries the session id, and which the workspace root, is not readable from a
minified bundle with any confidence. `identity()` has to be written against a
captured payload, so the implementation plan's first task is a probe that
installs into a scratch project and captures one raw payload per subscribed
hook. Everything else in this design is blocked on nothing.

### The contract test, in two parts

Mirroring `tests/test_opencode_hooks_contract.py`, and deliberately two tests
rather than one:

- **Always runs, everywhere, CI included.** Every hook named in the generated
  `hooks.json`, and every key of `EVENT_KINDS`, is in the vendored
  `HOOK_NAMES`. Needs nothing installed. This catches subscribing to a hook
  Cursor does not emit - the failure mode where an integration reports success
  for months while recording nothing.
- **`@pytest.mark.cursor`, may skip.** The vendored list still matches what
  Cursor.app enumerates. This guards only the freshness of remem's own copy.

Collapsing them into one produces a guard that skips on CI, which is the failure
the `db` markers already taught this project to distrust.

The freshness half reads the names by matching the hook enumeration in Cursor's
minified bundle. That is uglier than opencode's `index.d.ts` parse and it will
break on a bundler change - which is acceptable, because breaking is how a
skipping-or-failing freshness check is supposed to behave, and the always-runs
half does not depend on it. A crude reader for one known file beats a dependency
out of all proportion to the job, the same trade the opencode parser made.

Cursor auto-updates itself. Expect this test to fire, exactly as opencode's did
mid-branch when it upgraded from 1.3.5 to 1.17.7.

### Testing

- Unit: `hooks_path` per scope and its `UnsupportedScope`; the `hooks.json`
  merge preserving a foreign tool's entries and backing up first; `.mdc`
  rendering; the `.git/info/exclude` append being idempotent and its no-repo
  path; `event()` mapping each subscribed hook to its kind and returning `None`
  for everything else and for a payload with no session id.
- Contract: the two tests above.
- `db`: install and verify, as every adapter's install tests are.
- Payload seam: a test pinning the exact keys `event()` reads, the counterpart
  to `tests/test_opencode_event.py`. Nothing type-checks the boundary between a
  harness's payload and the adapter, and a rename on either side means a harness
  that records nothing, silently, while every other test stays green.

### End-to-end proof

Contract tests are not the bar. opencode was proven against the real harness -
real events in the database across real sessions, and the block found verbatim
in the prompt the model actually received - and Cursor gets the same treatment.

The obstacle is that the `cursor-agent` CLI installed on this machine is
`2025.09.12`, which predates hooks entirely, while Cursor.app 3.9.16 supports
them. The proof therefore requires updating `cursor-agent` to a hooks-capable
build first, then driving it with `cursor-agent -p`, the way `opencode run`
drove the opencode proof. Updating it runs a third-party network installer and
is asked before it is done.

What the proof must show, all four:

1. Events land, from a real session, with both `tool_call` and `message` kinds.
2. `.cursor/rules/remem.mdc` is written, with correct frontmatter.
3. The block reaches the model - confirmed from what was sent, not inferred from
   what came back.
4. The `.mdc` is written once per session, not once per turn.

## Out of scope

- **The `sessionEnd` hook**, and any change to the extraction trigger. Reasoned
  above.
- **Cursor's Claude Code compatibility layer.** Reasoned above.
- **Wiring extraction into the Cursor path.** `spawn_process` is called from
  exactly one place, Claude Code's `SessionStart` hook, so a Cursor-only install
  records but never extracts - the same real, undecided gap the opencode work
  left open. It is one gap for two adapters now, which strengthens the case for
  fixing it properly rather than bolting it onto a third install path.

  *Amended 2026-08-30: done, and fixed once rather than per adapter.
  `spawn_process` moved to `remem/hookio.py` and is now called from
  `remem hook context` as well as Claude Code's `SessionStart` - the
  session-start analogue opencode and Cursor already call once per session.
  A harness with no `claude` on PATH fails those jobs and records the reason
  rather than skipping them; the attempt cap stops the retries.*
- **`beforeReadFile`, `afterAgentThought`, and the specific `after*` tool
  hooks.** In the vendored list, not subscribed.
- **A Cursor MCP registration.** This design wires hooks. remem's MCP server is
  registered per harness and Cursor's mechanism is its own; it is separable and
  nothing here depends on it.
