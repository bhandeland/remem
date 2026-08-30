# Cursor adapter - what is proven, and what is not

Date: 2026-08-29
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
