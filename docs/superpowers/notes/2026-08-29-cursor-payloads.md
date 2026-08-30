# Cursor hook payload shapes - source-derived, not captured

## Provenance - read this before trusting anything below

**No payload in this note was captured from a live Cursor session.** The
original plan (Task 3's brief) was to drive a real `cursor-agent -p` session
against a scratch workspace with a capture hook and record the raw JSON. That
plan failed for a reason outside this project's control: the user has no
Cursor account. Both authentication paths were tried and both require an
account that does not exist here:

- `cursor-agent login` (CLI, freshly updated from `2025.09.12` to
  `2026.08.25-3e8eec8` via `curl https://cursor.com/install -fsS | bash`,
  approved by the user) prints a browser URL
  (`https://cursor.com/loginDeepControl?...`) and blocks waiting for a human
  to approve it against a real Cursor account.
- Cursor.app (3.9.16, already installed on this machine) has no session
  either, for the same reason.

No live session will run on this machine. Given that, the user approved a
fallback: read the payload-CONSTRUCTING code directly out of the installed
Cursor.app bundle
(`/Applications/Cursor.app/Contents/Resources/app/out/vs/workbench/workbench.desktop.main.js`,
Cursor 3.9.16 - the same version `agents/cursor/hooks.py` vendors
`HOOK_NAMES` from).

This is stronger evidence than prose documentation - it is the literal code
that assembles the JSON object Cursor writes to a hook's stdin - but it is
**not** a captured payload. Nobody has seen the bytes a real hook invocation
actually produces on this machine. Field names below could still be wrong if
this reading of the minified bundle is wrong, and nothing here confirms hook
*ordering*, or which extra fields each hook mixes into the common envelope.
Treat every line in the summary table as "source-derived", not "observed."

## The constructor, as read from the bundle

Every one of Cursor's hook payloads is assembled by one function. Paraphrased
back from the minified names to the extent they can be told apart (`e` is the
hook-specific field object, `n` is the hook name, `t` is the user's email,
`a` is the transcript path, `l` is the agent transcript path):

```js
d = {...e,                                  // hook-specific fields
     ...(c !== undefined && {session_id: c}),
     hook_event_name: n,
     cursor_version: this.productService.version,
     workspace_roots: this.workspaceContextService.getWorkspace().folders.map(D => D.uri.path),
     user_email: t,
     ...(!s && {transcript_path: a}),
     ...(n === subagentStop && {agent_transcript_path: l})}
```

where:

- `c = s ? undefined : r ?? i` - `r` is the hook-specific payload's own
  `session_id` field (if it set one), `i` is its own `conversation_id`. So
  `session_id` in the final envelope is `r` falling back to `i`, and it is
  present at all only when `s` is false.
- `s = oES(n)`, and `oES(n) = sty.includes(n)` where `sty = [workspaceOpen]`.
  So `s` is true for exactly one hook: `workspaceOpen`. For every other hook
  - including all four this adapter cares about - `s` is false, which means:
  - `session_id` is present (via `r ?? i`)
  - `transcript_path` is present (the `!s` spread fires)

None of `sessionStart`, `postToolUse`, `beforeSubmitPrompt`,
`afterAgentResponse` is `workspaceOpen`, so all four get both `session_id`
and `transcript_path` in the envelope, per this reading of `oES`/`sty`.

`workspace_roots` is unconditional and always a `.map()` over
`getWorkspace().folders` - i.e. **a list of path strings**, never a bare
string, for every hook including these four.

`hook_event_name` - not `hook` - carries the hook name itself, so the
capture script's own `"hook"` wrapper key (from the Task 3 brief's
`capture.sh`) was never going to collide with anything Cursor sends; it is a
distinct, adapter-added key.

For `postToolUse` specifically (also `preToolUse` and
`postToolUseFailure`), the bundle has a separate switch that reads the tool
name out of the hook-specific field object `e`:

```js
case "preToolUse": case "postToolUse": case "postToolUseFailure":
    return e.tool_name || void 0;
```

So the tool name key is `tool_name`, sitting in the same `e` spread that
becomes part of `d` above - i.e. it lands as a top-level `tool_name` key in
the `postToolUse` payload, not nested.

## Payload shape per hook (reconstructed, not captured)

No exact byte-for-byte JSON can be shown here - that is exactly the thing a
live capture would have produced and could not. What follows is the set of
keys each of the four hooks is constructed to carry, per the reasoning
above.

### `sessionStart`

Common envelope only, as far as the constructor shows (no hook-specific
switch case was found for this one during the read): `session_id`,
`hook_event_name: "sessionStart"`, `cursor_version`, `workspace_roots`
(list), `user_email`, `transcript_path`.

### `postToolUse`

Common envelope, plus `tool_name` from the hook-specific field object:
`tool_name`, `session_id`, `hook_event_name: "postToolUse"`,
`cursor_version`, `workspace_roots` (list), `user_email`,
`transcript_path`, and whatever other tool-call fields (arguments, result)
live in `e` that this read did not enumerate.

### `beforeSubmitPrompt`

Common envelope: `session_id`, `hook_event_name: "beforeSubmitPrompt"`,
`cursor_version`, `workspace_roots` (list), `user_email`,
`transcript_path`, plus whatever prompt-specific fields live in `e`
(presumably the prompt text) that this read did not enumerate.

### `afterAgentResponse`

Common envelope: `session_id`, `hook_event_name: "afterAgentResponse"`,
`cursor_version`, `workspace_roots` (list), `user_email`,
`transcript_path`, plus whatever response-specific fields live in `e` that
this read did not enumerate.

## Summary table

| question | key | provenance |
|---|---|---|
| session id | `session_id` (falls back to the payload's own `conversation_id` if unset) | source-derived, not captured |
| workspace root | `workspace_roots` - a **list** of path strings, never a bare string | source-derived, not captured |
| tool name (`postToolUse`) | `tool_name` | source-derived, not captured |
| hook name itself | `hook_event_name` (not `hook`) | source-derived, not captured |
| does `sessionStart` fire at all | **unverified** - constructor code exists for it, but no live invocation was ever observed on this machine | source-derived, not captured |
| transcript path | `transcript_path`, present for all four hooks (absent only for `workspaceOpen`) | source-derived, not captured |
| cursor version | `cursor_version` | source-derived, not captured |
| user email | `user_email` | source-derived, not captured - see privacy note below |

`workspace_roots` is built from `folders.map(D => D.uri.path)` - `uri.path`,
not `uri.fsPath`. On POSIX the two agree; on Windows `uri.path` yields a
form like `/c:/Users/...` where `uri.fsPath` would give `C:\Users\...`, and
`Path()` on the adapter side would mishandle the former. remem has not been
tested on Windows at all, and this is where a path difference would first
surface if it ever is - flagged here rather than fixed, since there is
nothing to verify it against on this machine.

## What remains genuinely unverified

- **Whether any of these four hooks fires at all.** The constructor code
  exists and is wired into a shared payload-building function that mentions
  all of them, but no hook invocation - real or synthetic - has actually run
  on this machine. `HOOK_NAMES` in `agents/cursor/hooks.py` was itself
  transcribed the same way (reading the bundle's self-naming enumeration),
  which raises the same caveat.
- **Firing order.** Nothing in this reading establishes whether
  `sessionStart` fires before `beforeSubmitPrompt`, whether `postToolUse`
  can fire multiple times per turn, or how `afterAgentResponse` relates to
  `stop`.
- **The hook-specific fields in `e`** for `sessionStart`,
  `beforeSubmitPrompt`, and `afterAgentResponse` beyond what the common
  envelope adds. Only `postToolUse`'s `tool_name` was traceable to a
  specific switch statement during this read; the rest of `e`'s shape for
  each hook was not enumerated.
- **Whether `session_id` is ever actually absent** for one of these four
  hooks in practice (i.e. whether `r` and `i` can both be unset) - the
  `oES`/`sty` reasoning says it should always be present, but that is a
  reading of minified control flow, not an observed value.
- **Exact JSON serialization details** - key ordering, whether any field is
  `null` vs. omitted, whether `workspace_roots` can be an empty list versus
  always having at least one entry.

## Privacy note for the adapter design (not acted on here)

`user_email` is part of the common envelope on every one of these hooks, per
the constructor above. remem's events design records events **in full** and
keeps them **indefinitely** (`services/record.py`, `remem events prune
--before` is the only deletion path) - so a Cursor adapter built against this
payload shape means every recorded Cursor event will contain the user's
email address, unlike the Claude Code adapter's events today. This is
flagged for whoever writes the Cursor adapter's `event()` function to decide
what to do with it (store as-is, strip it, or something else) - this task
does not decide that, only records that the field is there.

## Versions

- Cursor.app: 3.9.16 (the bundle this note reads from, and the same version
  `agents/cursor/hooks.py`'s `HOOK_NAMES` was vendored from).
- `cursor-agent` CLI: updated during this task from `2025.09.12-4852336` to
  `2026.08.25-3e8eec8` via `curl https://cursor.com/install -fsS | bash`.
  The update did not change the authentication blocker - a fresh CLI still
  needs a real Cursor account to run `-p`.
