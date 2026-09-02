# Hook install doctor - design

Date: 2026-09-01
Status: implemented (2026-09-01)
Builds on: docs/superpowers/specs/2026-08-28-events-and-recall-design.md
Follows: docs/superpowers/specs/2026-08-29-cursor-adapter-design.md

## Purpose

On 2026-08-30 remem discovered that Claude Code had recorded **zero** tool
calls for the entire life of the events pipeline. `~/.claude/settings.json`
held three hooks, not four: no `PostToolUse`. The adapter had been correct the
whole time - the installed config predated the events pipeline and
`remem install claude-code` had never been re-run.

Nothing surfaced it. Hooks are fail-soft and print nothing on error.
`remem record status` reports what *was* recorded and cannot know what should
have been. Extract jobs finished `done` with `entries_written=0`, which looks
exactly like a session with nothing worth extracting. The cost was an unknown
number of unrecorded sessions.

Commit 525b491 added a detector for a hook registered **twice**, inferred from
duplicate event rows. Nothing catches one registered **zero** times, which is
the more expensive failure and the more likely one: every rename, every new
hook added to an adapter, and every config written before a feature existed
produces it.

This design adds one question remem can answer and currently cannot: **does the
installed configuration actually register the hooks this adapter installs?**

## Scope

Hook registration only.

Not: the MCP registration, the skill file, Cursor's `.mdc` or its
`.git/info/exclude` entry, opencode's plugin freshness, whether the `remem` on
PATH is the install the hooks will execute, or any form of auto-repair.
`remem install <agent>` is already the fix, and each of those is a separate
question with a separate answer shape. Hook registration is the one that has
already cost real data.

## Approach

### The capability

Another optional adapter capability, probed with `getattr` exactly as
`event()`, `env_settings()`, `settings_path()` and `inject()` are, and
documented on the Protocol rather than declared on it - a third-party adapter
written before this existed must keep working.

A correction found while writing this: `base.py`'s comment block documents four
probed capabilities and CLAUDE.md calls `inject()` "the fourth", but `cli.py`
probes a fifth with `getattr` - `verify()` - which neither document mentions.
So this is the sixth, and the implementation should add `verify()` to the
Protocol's comment block while it is adding `hook_state()`. An undocumented
seam is how a third-party adapter author learns about a capability by
accident.

```python
def hook_state(
    self, scope: str, home: Path, env: Mapping[str, str]
) -> HookState: ...
```

The signature mirrors `install()`, for the same reasons: `env` is injected
rather than read from `os.environ` so tests can relocate an install without
mutating the real process, and `scope` is present because Cursor has two real
scopes and reads a different file for each.

It returns **facts, not verdicts**:

```python
@dataclass(frozen=True, slots=True)
class ExpectedHook:
    #: The harness's own hook name, spelled as the harness spells it.
    event: str
    #: The canonical remem command for that hook.
    command: str
    #: False means the install degrades without it; True means a core path
    #: stops working. Only the adapter knows which - see the table below.
    required: bool
    #: One line, rendered to the user, saying what stops working without it.
    provides: str


@dataclass(frozen=True, slots=True)
class HookState:
    expected: tuple[ExpectedHook, ...]
    #: The config file read, or None when it does not exist.
    path: Path | None
    #: Hook event -> the remem commands found registered for it, in file
    #: order. Repeats are preserved: two entries is the finding, not an
    #: implementation detail to collapse.
    found: Mapping[str, tuple[str, ...]]
```

`found` carries **only remem's own commands** - the canonical one and anything
in that adapter's `LEGACY_COMMANDS`. Another tool's hooks in a shared file are
not remem's business and never reach a report, the same rule `_install_hook`
already follows when it repairs entries.

Returning facts rather than findings is what keeps every adapter agreeing on
what "missing" means, and it is what lets one computation feed the full report,
the `--json` form and the `record status` advisory line.

### One table, two readers

The rule that the check and the installer cannot disagree is enforced by having
one table, not by keeping two in step. This is the shape
`extraction.awaiting_sessions` already uses for the attempt-cap rule: both
`remem events process` and `remem record status` route through it, so they
cannot disagree about which sessions are stuck.

**Claude Code**: the four-entry tuple currently written inline in
`_install_hook` moves to a module constant and gains `required` and `provides`.
It keeps its Claude-Code-specific `timeout`, which has no meaning for another
harness; `hook_state()` projects the table down to the generic `ExpectedHook`.
The generic type stays generic and the table stays single.

**Cursor**: `install.ENTRIES` is already that constant. It gains the same two
fields.

**opencode**: does not implement `hook_state()`. It has no hook configuration -
`install()` overwrites a plugin file wholesale - so there is no registration to
check. See "Unchecked is not OK" below.

### Required-ness

Not every hook is load-bearing, and the difference is adapter knowledge:

| adapter | hook | required | what is lost without it |
|---|---|---|---|
| claude-code | `SessionStart` | yes | context injection, and nothing spawns `events process` |
| claude-code | `PostToolUse` | yes | nothing is recorded at all - the original bug |
| claude-code | `UserPromptSubmit` | no | the handoff size warning |
| claude-code | `SessionEnd` | no | "a hint, not a requirement" per its own comment - extraction runs on an idle timer |
| cursor | `sessionStart` | yes | injection, and the only extraction trigger a Cursor-only install has |
| cursor | `postToolUse` | yes | no tool calls recorded |
| cursor | `beforeSubmitPrompt` | yes | prompts missing from extraction input |
| cursor | `afterAgentResponse` | yes | responses missing from extraction input |

Cursor's two message hooks are required, and that is a change of mind worth
recording. The first draft marked them optional on the grounds that losing
them costs extraction *quality* rather than recording itself. The
render-budget work of 2026-08-31 (commit 8cb186c) measured that distinction
away: on one 112-event session, showing the extractor a fragment returned
nothing in three runs where showing it the whole session returned entries in
five of five. Coverage is what the extractor needs, and a Cursor install
missing both message hooks records only tool calls - a session with its
prompts and its answers cut out. That is a fragment, and the measurement says
a fragment yields nothing.

Claude Code's `SessionEnd` stays optional for a different reason that survives
the same argument: it loses no events at all, only the promptness of the idle
timer. `UserPromptSubmit` stays optional because the handoff warning is not
part of the record-and-extract path.

### The service

`services/doctor.py` owns every judgement. Frontends parse and format; they
never decide.

Verdicts:

- `OK` - the canonical command is registered exactly once.
- `MISSING` - no remem command for this hook.
- `DUPLICATED` - more than one. This catches at the source what 525b491
  detects downstream by inferring from duplicate event rows, and it catches it
  before a single duplicate row is written.
- `STALE` - a `LEGACY_COMMANDS` string is registered where the canonical
  command should be. `install()` would rewrite it; until then the hook may
  still fire, so this is a warning and not a failure.
- `UNCHECKED` - the adapter does not implement the capability, or its
  implementation raised.

Two rulings the service enforces:

**"Installed" means at least one remem command appears in that harness's
config.** Zero remem commands is *not installed*: doctor prints one line
saying so for that adapter and reports no per-hook verdicts, and
`record status` says nothing at all. Warning that Cursor's hooks are missing on a machine with no Cursor would
make the whole report noise, and a report people learn to skim is a report that
hides the next `PostToolUse`. Some-but-not-all is exactly the bug being caught,
and is never quiet.

**Unchecked is not OK.** An adapter without the capability reports `UNCHECKED`
and says "no hook registration to check", never "ok". Reporting success for
something never verified is the precise failure this feature exists to prevent:
it is what claude-mem's opencode integration did for months, it is what the
extract jobs did with `entries_written=0`, and `agents/verify.py` already
carries that lesson in its docstring.

### Degradation

The registry contract is that a broken third-party adapter warns rather than
breaking remem. A `hook_state()` that raises lands where a missing one lands:
warn, report `UNCHECKED` for that adapter, and keep going. `remem doctor` must
complete over every other adapter, and `remem record status` must still print
its database report. Same contract as `agents/registry.discover` and as
`services/settings.resolve_targets`.

### Frontends

**`remem doctor [agent]`** - every adapter by default, one if named.
`--scope` defaults to `user` and mirrors `install()`'s, which is the scope
every adapter supports; Cursor's `project` scope is reachable with the flag.
`--json` for the machine form, which carries the same verdicts and the same
exit code as the human form.

Exits **1 when a required hook is missing on any adapter**, 0 otherwise. A
`STALE` or `DUPLICATED` verdict does not fail the command - the hook still
fires - and neither does `UNCHECKED`, which reports the absence of a check
rather than the presence of a fault. Exiting non-zero for "I could not tell"
would train users to ignore the exit code, and the exit code is the half a
script reads.
Fail-loud, because this is a command the user typed and its entire purpose is
to answer a question whose wrong answer is silence; the fail-soft contract
covers hooks, which must never be why a session will not start, and this is not
one.

```
$ remem doctor claude-code
claude-code  ~/.claude/settings.json
  SessionStart       ok
  SessionEnd         ok
  PostToolUse        MISSING  - nothing is recorded at all
  UserPromptSubmit   ok

  Fix: remem install claude-code
```

**`remem record status`** gains one advisory line per installed-but-incomplete
adapter, naming the hook and pointing at `remem doctor <agent>`. This is where
it earns its keep: `record status` is what a user runs when a harness looks
quiet, and today it is structurally incapable of answering. It is also the only
place that can reach a harness which has recorded *nothing ever* and therefore
appears nowhere in `event_stats`.

The filesystem half is wrapped: an unreadable config warns rather than taking
down a status command that is otherwise about the database.

## Testing

Following the shape the opencode and Cursor hook-contract tests established -
the half that must always run is separated from the half that may skip.

- `hook_state` against a `settings.json` missing `PostToolUse`; one with it
  registered twice; one naming a legacy command; one that does not exist at
  all. Cursor equivalents against `hooks.json`, both scopes.
- `found` ignores another tool's hook on the same event.
- Service verdict policy per case, including quiet-when-not-installed.
- An adapter whose `hook_state` raises: warns, reports `UNCHECKED`, and
  `doctor` still completes over the others. Mutation-checked - the guard is
  watched failing before it is trusted.
- `record status` prints the advisory line when a required hook is missing and
  does not when the install is complete.
- The hook names doctor expects are asserted against **literals written in the
  test**, not against the constant itself. A test that builds its fixtures from
  the code's own constants proves nothing; this is the guard that would have
  caught the original missing `PostToolUse`, so it must not be able to pass by
  agreeing with a table that is itself wrong.

## What this does not close

A hook that is registered, current, and unique can still record nothing: the
`remem` it names may not be on PATH, or may resolve to a different install than
the developer expects. `remem verify <agent>` already round-trips the database
to prove recording works end to end, and the two commands answer different
halves - `doctor` asks whether the harness will ever call remem, `verify` asks
whether remem works when called. Neither subsumes the other and this design
does not merge them.
