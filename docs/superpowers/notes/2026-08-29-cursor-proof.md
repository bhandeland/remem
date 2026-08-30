# Cursor adapter - what is proven, and what is not

Date: 2026-08-29
Amended: 2026-08-30 - a live Cursor session finally ran. See "2026-08-30: the
live session" at the end, which supersedes the two sections immediately below.
Everything from "The one fact that matters most" through "What WOULD constitute
proof" is left exactly as written on 2026-08-29, because a record of what was
believed before the evidence arrived is worth more than a tidy document.
Covers: Task 7 of `docs/superpowers/sdd/2026-08-29-cursor-adapter/` (the implementation plan)
Supersedes: the end-to-end proof section of `docs/superpowers/specs/2026-08-29-cursor-adapter-design.md`,
which assumed updating `cursor-agent` would unblock a live session. It did not.

## The one fact that matters most

**The Cursor adapter has never recorded a real event from a real Cursor
session.** No hook has been observed to fire, on this machine or anywhere
else reachable from it. This is not something anyone forgot to check - it is
a hard blocker: there is no Cursor account on this machine, and neither
`cursor-agent -p` nor Cursor.app can authenticate without one. `cursor-agent
login` prints a browser URL and blocks on human approval against a real
account; Cursor.app hits the same wall. Updating `cursor-agent` (done during
Task 3, `2025.09.12-4852336` -> `2026.08.25-3e8eec8`) did not remove the
blocker, because the blocker was never the CLI's age - it is the missing
account.

Every payload key the adapter reads - `session_id`, `workspace_roots`,
`hook_event_name`, `tool_name` - is derived from reading Cursor's own
payload-constructing code inside the installed app bundle
(`Cursor.app` 3.9.16,
`Contents/Resources/app/out/vs/workbench/workbench.desktop.main.js`), and
that reading was done a second time, independently, before being trusted
(see `docs/superpowers/notes/2026-08-29-cursor-payloads.md`, whose provenance
section is the primary record of this). It has never been confirmed against
a live payload. Source code that assembles a JSON object is strong evidence
of the object's shape, but it is not the object. Field names here could
still be wrong in a way that a live capture would have caught immediately.

## What this note DOES prove, with evidence

Everything below was run in a scratch directory outside the repository
(`/private/tmp/cursor-proof-adapter`, a throwaway `git init`), against this
branch's own `remem` (the editable install on `PATH` resolves to
`/Users/brandon/llmworkspace/remem/.claude/worktrees/cursor-adapter/src/remem`,
confirmed before running anything below).

### The agent argument is positional, not a flag

```
$ remem install --agent cursor --scope project
Usage: remem install [OPTIONS] [agent]
Try 'remem install --help' for help.
╭─ Error ──────────────────────────────────────────────────────────────────────╮
│ No such option: --agent                                                      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

The correct invocation:

```
$ remem install cursor --scope project
  Merged 4 hook entries into /private/tmp/cursor-proof-adapter/.cursor/hooks.json
  Verified the install with a live round-trip: recorded, read back, and deleted a test event

Installed remem for cursor.
Recording is OFF until you enable it per project: `remem record enable --project <name>`. ...
The knowledge base block is written to .cursor/rules/remem.mdc at every session start, and added to .git/info/exclude so it stays out of git.
```

Exit code 0.

### `hooks.json` gets exactly the four entries the design specifies

```json
{
  "version": 1,
  "hooks": {
    "sessionStart": [{"command": "remem hook context --agent cursor"}],
    "postToolUse": [{"command": "remem record event --agent cursor"}],
    "beforeSubmitPrompt": [{"command": "remem record event --agent cursor"}],
    "afterAgentResponse": [{"command": "remem record event --agent cursor"}]
  }
}
```

Four hooks, one command each. Every command names `--agent cursor` correctly
(not the Claude-Code-only `remem hook record-event`).

### The live database round-trip in `verify()` passes

The install output above includes "Verified the install with a live
round-trip: recorded, read back, and deleted a test event" with a 0 exit
code - `agents/verify.round_trip` actually reached Postgres, wrote a row,
read it back, and deleted it. This is real evidence that remem's own half of
the pipeline (recording an event once one arrives) works. It says nothing
about whether Cursor will ever hand that pipeline an event.

### Re-running the install does not duplicate entries

Running `remem install cursor --scope project` a second time in the same
directory:

```
$ remem install cursor --scope project
  Merged 4 hook entries into /private/tmp/cursor-proof-adapter/.cursor/hooks.json
  Backed up the previous file to /private/tmp/cursor-proof-adapter/.cursor/hooks.json.bak1788050883
  Verified the install with a live round-trip: recorded, read back, and deleted a test event
```

`hooks.json` afterward is byte-identical in structure to the first run -
still four hooks, one entry each, no doubled commands. The merge in
`agents/cursor/install.py` is idempotent by command string, and it backs up
the previous file before writing, on every run, exactly as the design
specifies.

### What this half proves, and what it does not

This proves the install path and the record path work: the CLI writes the
right file in the right shape, merges without duplicating, backs up before
overwriting, and the database round-trip that recording depends on
succeeds. **It proves nothing about whether Cursor ever calls those hooks,
with what payload shape, or at what frequency.** That gap is the rest of
this note.

## What is NOT proven, and why

Steps 3, 4 and 5 of the original task brief - drive a real Cursor session,
confirm events of both kinds landed, confirm the `.mdc` was written with the
marker, confirm the block reached the model (not just its reply), confirm
the file is written once per session and not once per turn - could not be
attempted. All five require a Cursor session to actually run, and no Cursor
session can authenticate on this machine. Nothing here was faked or
approximated in their place; they are recorded as not done.

## What WOULD constitute proof

For whoever next has a Cursor account and a login:

1. **Both kinds of events, from one real session.** `remem record enable
   --project <name>` in a real Cursor workspace with this adapter installed,
   then a real chat turn that both submits a prompt and calls a tool.
   `remem events show --project <name>` should show at least one
   `EventKind.MESSAGE` and one `EventKind.TOOL_CALL`, each carrying the
   payload keys this note assumes (`session_id`, `hook_event_name`,
   `tool_name`, `workspace_roots`). Any missing or renamed key here is the
   single highest-value thing a live session could find, because
   `agents/cursor/install.py`'s `SESSION_KEY`/`ROOT_KEY` constants are the
   entire seam between a harness that records everything and one that
   silently records nothing.
2. **The `.mdc` written with correct frontmatter.** `cat
   <workspace>/.cursor/rules/remem.mdc` after a session start - expect the
   `alwaysApply: true` frontmatter block from `agents/cursor/rules.py` and
   the rendered knowledge base body beneath it.
3. **The block confirmed present in what the model was actually sent**, not
   inferred from its reply. A model can mention something it inferred rather
   than something it was actually given; this has to come from a request
   log the model server keeps, or from asking the session a question only
   the injected block could answer and cross-checking against the
   provider's own record of the request, not the reply text alone.
4. **Written once per session, not once per turn.** Two turns inside one
   session; the `.mdc`'s mtime should not advance on the second turn, since
   only `sessionStart` triggers `inject()` and it fires once per session by
   construction (see the design's "Once per session, by construction"
   section).

None of the four is proven here. All four are named, not silently dropped.
An adapter shipping unproven against its actual target is a real risk to
carry forward, and this note exists so that risk is recorded rather than
hidden by contract-test coverage that never touched a live harness.

---

## 2026-08-30: the live session

A Cursor account was linked on this machine, and the adapter recorded real
events for the first time. This section supersedes "The one fact that matters
most" and "What is NOT proven, and why" above. Cursor had auto-updated from
3.9.16 to 3.18.9 by the time the session ran; the vendored `HOOK_NAMES`
freshness test (`@pytest.mark.cursor`) still passes against the new bundle, so
the hook list is unchanged across that jump. Only the `CURSOR_VERSION = "3.9.16"`
label in `agents/cursor/hooks.py` is now stale - the data it annotates is not.

### Criterion 1 - both event kinds, with the assumed payload keys: PROVEN

Nine events across two turns of one session, `remem record status` reporting a
`cursor` row for the first time:

```
   kind    | tool  |    at
 message   |       | 12:56:06
 tool_call | Grep  | 12:56:11
 tool_call | Shell | 12:56:13
 message   |       | 12:56:16
 message   |       | 17:10:24
 tool_call | Shell | 17:10:29
 tool_call | Grep  | 17:10:30
 tool_call | Shell | 17:10:32
 message   |       | 17:10:39
```

All four subscribed hooks fired and routed correctly: `beforeSubmitPrompt` and
`afterAgentResponse` to `EventKind.MESSAGE`, `postToolUse` to
`EventKind.TOOL_CALL`. Every key this adapter guessed from the minified bundle
is confirmed against live payloads - `session_id` populated and stable across a
session, `workspace_roots` non-empty and resolving to project `remem`,
`hook_event_name` driving `EVENT_KINDS`, and `tool_name` yielding real names
(`Grep`, `Shell`). `SESSION_KEY`/`ROOT_KEY` - which this note called the entire
seam between recording everything and silently recording nothing - are right.
The source reading held up.

`user_email` is present on every payload, as documented, and is now genuinely
stored in the events table.

### Criterion 2 - the `.mdc` written with correct frontmatter: PROVEN

```
---
description: remem knowledge base
alwaysApply: true
---
<!-- generated by remem - do not edit. Rewritten at every session start. -->
```

15,699 bytes. `.git/info/exclude` gained `**/.cursor/rules/remem.mdc` - the
`**/`-anchored form from the worktree fix - and `git status` does not see the
file, so the working-tree write stays out of the user's diffs.

**But it failed on the first live attempt, and the reason is the most valuable
thing this session found.** `remem hook context --agent cursor` ran with exit
code 0 and wrote nothing. With `REMEM_HOOK_DEBUG=1`:

```
remem hook: RulesExceedBudget: rules and header need 12044 chars, budget is 6000
```

`inject()` raised, and the fail-soft contract turned that into a silent exit 0.
This is not a Cursor bug: `RulesExceedBudget` lives in `services/kb.py` against
`DEFAULT_MAX_CHARS`, and Claude Code's `remem hook session-start` failed
identically when probed. **Context injection had been silently dead on every
harness** once the knowledge base outgrew 6,000 chars. Raising `REMEM_MAX_CHARS`
to 16,000 fixed all harnesses at once.

The rendered block is 15,699 chars against that 16,000 budget - about 2% of
headroom. It will fail again, silently, in the same way. Pruning the knowledge
base is the durable fix; the raise bought room, not a solution.

### Criterion 3 - the block confirmed in what the model was SENT: STILL OPEN

Cursor's `cursor.requestTraces.log` holds only span names and timings (123
`span_completed`, 121 `span_started`) and zero request bodies - grepping it for
`alwaysApply` or any knowledge-base text returns nothing. No local Cursor log
records an outbound request body, so this criterion cannot be closed from the
machine's own logs. The only route that would actually satisfy it is a
TLS-intercepting proxy in front of Cursor, which means terminating TLS on
authenticated account traffic. Not done, deliberately.

Judgement, recorded so the next reader can disagree with it: this is now the
least valuable of the four. It guarded against a block that is written but never
reaches the model, and the concrete failure it was standing in for - wrong
payload keys, silently recording nothing - is exactly what criterion 1 has now
disproved with live data. `alwaysApply: true` is Cursor's documented mechanism,
but "Cursor's docs say so" is a claim about Cursor, not an observation, and this
note does not treat it as one.

### Criterion 4 - once per session, not once per turn: HALF PROVEN

**Not once per turn: proven live.** Two turns ran in session
`0f1d9067-ea42-48aa-8677-7bdcbb0b15b6`, the second more than four hours after the
first. The `.mdc` mtime did not move across the second turn. `inject()` does not
run per-message, which was the real risk - a rules file rewritten every turn
would churn the working tree continuously.

**Rewritten at a new session start: not observed live.** Both turns landed in the
same session, so no second `sessionStart` ever fired. That half rests on a manual
replay of a captured `sessionStart` payload plus the construction argument:
`hooks.json` wires `sessionStart` alone to `remem hook context`, the other three
steps to `remem record event`, and the live log confirms Cursor fires
`sessionStart` once per session. Construction plus inference, not observation.
One genuinely new chat would close it.

### Two findings that are not about Cursor

**Cursor loads Claude Code's hook configuration.** From the hooks service log:

```
Claude user config path: /Users/brandon/.claude/settings.json
Loaded Claude user hooks
Executing hook 1/2 from claude-user config
Command: remem hook session-start (193ms) exit code: 0
```

Cursor 3.x reads `~/.claude/settings.json` (and a project's `.claude/settings.json`
and `.claude/settings.local.json`) and runs those hooks itself, handing them
Cursor payloads. So remem's Claude-Code hooks - `session-start`, `session-end`,
`session-size` - already execute inside Cursor, receive a shape they were never
written to parse, and exit 0 in silence. Nothing is broken by this today. It is
undecided territory, and it means the two adapters are less independent than the
registry seam suggests. It also means a Cursor-only user who has never installed
the Claude Code adapter is unaffected, while a user with both gets every hook
twice, by two different routes.

**Fail-soft hides the one failure a user would want to see.** The budget failure
above cost every harness its context block and produced no signal anywhere except
under `REMEM_HOOK_DEBUG=1`. The contract is still right - a knowledge tool must
not be why a session will not start - but "the block was computed and then thrown
away" is categorically different from "the database was down", and only the second
is the kind of failure silence was designed for.

### Corrections to this note's own instructions

The "What WOULD constitute proof" section above prescribes
`remem events show --project <name>`. **That command does not exist** - `events
show` takes an entry id and is the forensic lookup from an entry back to its raw
events. There is no CLI that lists raw events by project; the tables above came
from SQL against the `events` table directly. Either add such a command or amend
that sentence.

### Debugging record - why the first three attempts recorded nothing

Kept because each failure looked exactly like a broken adapter and none was:

1. **No workspace folder open.** Cursor loads project hooks only when a folder is
   open; the window was empty (`No workspace folder found, project hooks will not
   be available`), so `.cursor/hooks.json` was never read. Payloads carried
   `workspace_roots: []`.
2. **Two windows.** The folder opened in a new window while the chat turn was
   typed into the old empty one. The folder window logged `Loaded 4 project
   hook(s)` and then saw no chat session at all.
3. **Resolved by a full restart** (Cmd+Q, reopen the folder as the only window),
   which also picked up the 3.9.16 -> 3.18.9 update.

Two hypotheses were killed by evidence rather than by fixes, and both are worth
recording as *not* the problem: `remem` resolves fine from Cursor's GUI
environment (hooks ran with exit code 0 and non-zero durations, so PATH is not an
issue), and the project `hooks.json` is found and parsed with exactly the four
steps installed.
