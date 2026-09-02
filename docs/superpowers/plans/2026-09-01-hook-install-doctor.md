# Hook Install Doctor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a hook registered *zero* times visible, by teaching each adapter to report what its config actually registers and having a service judge it.

**Architecture:** A probed optional adapter capability `hook_state()` returns facts (what this adapter expects, what is on disk). `services/doctor.py` turns those facts into verdicts. Two frontends read the verdicts: a new `remem doctor`, and one advisory line inside `remem record status`. One table per adapter is read by both `install()` and `hook_state()`, so the installer and the check cannot drift.

**Tech Stack:** Python 3.14, Typer, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-01-hook-install-doctor-design.md`

## Global Constraints

- Python 3.14. `from __future__ import annotations` at the top of every module.
- Comments explain **why**, at length, especially where a decision looks arbitrary. Match the surrounding density; a subtle invariant with no comment reads as an accident to the next reader.
- In prose, comments and docs: spaced hyphens ` - `, never em dashes.
- Strict layering. Frontends parse and format, they never decide. Every policy decision lives in `services/`.
- TDD. Write the failing test, run it, watch it fail for the right reason, then implement.
- **Watch every new guard fail before trusting it.** Where a step says to mutation-check, break the production code deliberately, confirm the test fails, then restore.
- Run the suite with `uv run pytest -m "cursor or not cursor" -q`. A bare `-m cursor` deselects everything else. **Check the skip count: a green run with skips is not a green run.** Baseline at the start of this plan: 778 passed, 0 skipped.
- `remem doctor` must work with Postgres down. It reads files; it opens no database connection. `services/settings.py` is the existing precedent.
- Never report `ok` for something that was not checked.

---

### Task 1: The shared types, and Claude Code's hook table hoisted

Today the four hook entries live as a tuple literal inside `_install_hook`, where nothing else can read them. This task lifts them to a module constant carrying two new fields, and adds the types the capability will return.

**Files:**
- Modify: `src/remem/agents/base.py`
- Modify: `src/remem/agents/claude_code/adapter.py:181-270` (`_install_hook`)
- Test: `tests/test_claude_code_events_install.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `remem.agents.base.ExpectedHook`, `remem.agents.base.HookState`; `remem.agents.claude_code.adapter.HOOK_ENTRIES: tuple[ClaudeHook, ...]` and `ClaudeHook.expected() -> ExpectedHook`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_claude_code_events_install.py`. The hook names are **literals here on purpose** - a test that builds its fixtures from the code's own constants proves nothing, and this is the guard that would have caught the missing `PostToolUse`.

```python
def test_the_hook_table_names_every_hook_the_install_registers():
    """The table install() reads and the table hook_state() reads are one
    table. Literals, not the constant: a guard that agrees with a wrong
    table is how PostToolUse went missing for the life of the pipeline."""
    from remem.agents.claude_code.adapter import HOOK_ENTRIES

    assert [h.event for h in HOOK_ENTRIES] == [
        "SessionStart", "SessionEnd", "PostToolUse", "UserPromptSubmit",
    ]


def test_only_the_hooks_that_lose_events_are_required():
    """SessionEnd loses no events, only the promptness of the idle timer.
    UserPromptSubmit is the handoff warning, not the record path."""
    from remem.agents.claude_code.adapter import HOOK_ENTRIES

    required = {h.event for h in HOOK_ENTRIES if h.required}
    assert required == {"SessionStart", "PostToolUse"}


def test_every_expected_hook_says_what_is_lost_without_it():
    from remem.agents.claude_code.adapter import HOOK_ENTRIES

    assert all(h.provides.strip() for h in HOOK_ENTRIES)
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/test_claude_code_events_install.py -q -k "hook_table or only_the_hooks or says_what_is_lost"`
Expected: FAIL with `ImportError: cannot import name 'HOOK_ENTRIES'`.

- [ ] **Step 3: Add the shared types to `agents/base.py`**

Put these immediately after the `InstallReport` dataclass.

```python
@dataclass(frozen=True, slots=True)
class ExpectedHook:
    """One hook remem registers, as an adapter declares it.

    Generic on purpose: it carries nothing harness-specific. Claude Code's
    per-hook timeout has no meaning to Cursor, so it stays on that
    adapter's own table and is projected away here.
    """

    #: The harness's own hook name, spelled as the harness spells it.
    event: str
    #: The canonical remem command for that hook.
    command: str
    #: False means the install degrades without it; True means a core path
    #: stops working. Only the adapter knows which.
    required: bool
    #: One line, rendered to the user, naming what stops working without it.
    provides: str


@dataclass(frozen=True, slots=True)
class HookState:
    """What an adapter expects, and what it found on disk. Facts, not
    verdicts - `services/doctor.py` decides what counts as a problem, so
    that every adapter agrees on what "missing" means and so one
    computation feeds the report, the --json form and the `record status`
    advisory line."""

    expected: tuple[ExpectedHook, ...]
    #: The config file read, or None when it does not exist.
    path: Path | None
    #: Hook event -> the remem commands found registered for it, in file
    #: order. Repeats are preserved: two entries is the finding, not an
    #: implementation detail to collapse. Only remem's own commands appear
    #: here - another tool's hooks in a shared file are not remem's
    #: business, the same rule `_install_hook` follows when it repairs.
    found: Mapping[str, tuple[str, ...]]
```

- [ ] **Step 4: Document the capability on the Protocol**

In `agents/base.py`, inside the `AgentAdapter` comment block of optional capabilities, add these two entries. **`verify()` is already probed by `cli.py` and documented nowhere** - add it here in the same edit.

```python
    #     def verify(
    #         self, env: Mapping[str, str] | None = None, home: Path | None = None
    #     ) -> InstallReport: ...
    #
    # Record an event, read it back, delete it - see `agents/verify.py`.
    # Probed by `remem verify`; an adapter without it is reported as having
    # nothing to verify rather than as verified.
    #
    #     def hook_state(
    #         self, scope: str, home: Path, env: Mapping[str, str]
    #     ) -> HookState: ...
    #
    # What this adapter's config actually registers, against what it
    # installs. Returns facts; `services/doctor.py` judges them. The pair
    # with verify() is deliberate and neither subsumes the other:
    # hook_state asks whether the harness will ever call remem, verify asks
    # whether remem works when called. An adapter with no hook
    # configuration at all - opencode ships a plugin file instead - simply
    # does not implement it, and the report says "no hook registration to
    # check", never "ok". Same degradation contract as the rest: a
    # capability that raises warns and continues.
```

- [ ] **Step 5: Hoist Claude Code's table**

In `src/remem/agents/claude_code/adapter.py`, add above the `ClaudeCodeAdapter` class:

```python
@dataclass(frozen=True, slots=True)
class ClaudeHook:
    """One hook entry, as this adapter installs it.

    Carries `timeout`, which `ExpectedHook` deliberately does not: it is a
    fact about Claude Code's hook runner and means nothing to another
    harness. `expected()` projects it away.
    """

    event: str
    command: str
    timeout: int
    required: bool
    provides: str

    def expected(self) -> ExpectedHook:
        return ExpectedHook(
            event=self.event,
            command=self.command,
            required=self.required,
            provides=self.provides,
        )


#: The one table. `_install_hook` writes from it and `hook_state` checks
#: against it, so the installer and the check cannot disagree about which
#: hooks exist - the same reason `extraction.awaiting_sessions` is the only
#: place the attempt-cap rule is evaluated.
HOOK_ENTRIES: tuple[ClaudeHook, ...] = (
    ClaudeHook(
        "SessionStart", HOOK_COMMAND, 10, True,
        "context injection, and the spawn that drains the extraction backlog",
    ),
    # A hint, not a requirement: extraction runs on an idle timer now, so a
    # harness with no SessionEnd loses no events at all - only the
    # promptness of the timer. It records through the same command and the
    # same event() mapping as PostToolUse.
    ClaudeHook(
        "SessionEnd", RECORD_EVENT_COMMAND, 10, False,
        "a prompt end-of-session record; the idle timer covers it either way",
    ),
    # Runs once per tool call and does one INSERT, so it gets the short
    # budget UserPromptSubmit has, not the 10s SessionStart needs.
    ClaudeHook(
        "PostToolUse", RECORD_EVENT_COMMAND, 5, True,
        "every tool call - without it nothing is recorded at all",
    ),
    # Runs on every prompt, so it gets the shortest timeout of the four; it
    # reads one file and never opens Postgres.
    ClaudeHook(
        "UserPromptSubmit", SESSION_SIZE_COMMAND, 5, False,
        "the handoff size warning on long sessions",
    ),
)
```

Add `from dataclasses import dataclass` and `ExpectedHook` to the imports from `remem.agents.base`.

- [ ] **Step 6: Make `_install_hook` read the table**

Replace the inline tuple at `adapter.py:190-206` with the table. Only the loop header changes; the body is untouched.

```python
        for entry in HOOK_ENTRIES:
            event, command, timeout = entry.event, entry.command, entry.timeout
            groups = hooks.setdefault(event, [])
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_claude_code_events_install.py -q`
Expected: PASS, including the pre-existing install tests - the hooks written must be unchanged.

- [ ] **Step 8: Mutation-check the guard**

Delete the `PostToolUse` entry from `HOOK_ENTRIES`. Run the same tests.
Expected: `test_the_hook_table_names_every_hook_the_install_registers` FAILS. Restore the entry and confirm green again. A guard nobody has watched fail is not a guard.

- [ ] **Step 9: Commit**

```bash
git add src/remem/agents/base.py src/remem/agents/claude_code/adapter.py tests/test_claude_code_events_install.py
git commit -m "Hoist Claude Code's hook table so a check can read it too

The four entries lived as a tuple literal inside _install_hook, where
nothing else could see them. They now carry required-ness and a line
saying what is lost without each hook, and install() reads the same table
a check will."
```

---

### Task 2: `ClaudeCodeAdapter.hook_state`

**Files:**
- Modify: `src/remem/agents/claude_code/adapter.py`
- Test: `tests/test_claude_code_hook_state.py` (create)

**Interfaces:**
- Consumes: `HOOK_ENTRIES`, `ExpectedHook`, `HookState` from Task 1; `LEGACY_COMMANDS` and `resolve_paths`, both already in this module.
- Produces: `ClaudeCodeAdapter.hook_state(scope, home, env) -> HookState`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_claude_code_hook_state.py`:

```python
"""What the installed settings.json actually registers.

The bug this exists for: ~/.claude/settings.json held three hooks, not
four - no PostToolUse - and Claude Code recorded zero tool calls for the
entire life of the events pipeline. Hooks are fail-soft, so nothing said
so.
"""

from __future__ import annotations

import json

from remem.agents.claude_code.adapter import ClaudeCodeAdapter


def write_settings(home, hooks):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}))
    return path


def entry(command, timeout=5):
    return {"matcher": "", "hooks": [{"type": "command", "command": command,
                                      "timeout": timeout}]}


def test_a_hook_that_is_not_registered_is_reported_as_absent(tmp_path):
    write_settings(tmp_path, {
        "SessionStart": [entry("remem hook session-start")],
        "SessionEnd": [entry("remem hook record-event")],
        "UserPromptSubmit": [entry("remem hook session-size")],
    })
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert state.found["PostToolUse"] == ()
    assert state.found["SessionStart"] == ("remem hook session-start",)


def test_a_hook_registered_twice_reports_both(tmp_path):
    """525b491 infers this downstream from duplicate event rows. Read off
    the file it is visible before a single duplicate row is written."""
    write_settings(tmp_path, {
        "PostToolUse": [entry("remem hook record-event"),
                        entry("remem hook record-event")],
    })
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert state.found["PostToolUse"] == (
        "remem hook record-event", "remem hook record-event",
    )


def test_a_legacy_command_is_reported_as_the_command_it_actually_names(tmp_path):
    """`remem hook session-end` is a real back-compat alias, so the hook
    still fires. Reporting it as MISSING would be a lie; the service calls
    it STALE."""
    write_settings(tmp_path, {"SessionEnd": [entry("remem hook session-end")]})
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert state.found["SessionEnd"] == ("remem hook session-end",)


def test_another_tool_s_hook_on_the_same_event_is_not_remem_s_business(tmp_path):
    write_settings(tmp_path, {
        "PostToolUse": [entry("some-other-tool --hook"),
                        entry("remem hook record-event")],
    })
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert state.found["PostToolUse"] == ("remem hook record-event",)


def test_no_settings_file_at_all_is_an_answer_not_an_error(tmp_path):
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert state.path is None
    assert all(v == () for v in state.found.values())


def test_unreadable_settings_are_not_rewritten(tmp_path):
    """A check must never write. jsonfile.read_json backs a corrupt file up
    before returning {}, which is right for an install and wrong here."""
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json")
    before = sorted(p.name for p in path.parent.iterdir())
    ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert sorted(p.name for p in path.parent.iterdir()) == before


def test_the_expected_set_is_every_hook_the_adapter_installs(tmp_path):
    state = ClaudeCodeAdapter().hook_state("user", tmp_path, {})
    assert [h.event for h in state.expected] == [
        "SessionStart", "SessionEnd", "PostToolUse", "UserPromptSubmit",
    ]


def test_opencode_declines_the_capability_rather_than_answering_ok():
    """opencode ships a plugin file, not hook configuration, so there is no
    registration to check. It must DECLINE - an adapter that answered with
    an empty expected set would render as a clean bill of health for
    something never examined."""
    from remem.agents.opencode.adapter import OpencodeAdapter

    assert not hasattr(OpencodeAdapter, "hook_state")
```

Check the class name against `src/remem/agents/opencode/adapter.py` before writing this test and use whatever it actually is.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_claude_code_hook_state.py -q`
Expected: FAIL with `AttributeError: 'ClaudeCodeAdapter' object has no attribute 'hook_state'`.

- [ ] **Step 3: Implement**

Add to `ClaudeCodeAdapter`, next to `verify`:

```python
    def hook_state(
        self,
        scope: str = "user",
        home: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> HookState:
        """What settings.json actually registers, against HOOK_ENTRIES.

        Read-only, and deliberately not via `jsonfile.read_json`: that
        helper backs a corrupt file up before returning an empty document,
        which is correct for an install about to rewrite it and wrong for
        a check. A diagnostic that leaves .bak files behind is a
        diagnostic people stop running. Unreadable settings read as no
        remem hooks at all, which is the honest answer - the file names
        none that can be found.
        """
        if scope != "user":
            raise UnsupportedScope(
                f"scope '{scope}' is not supported; only 'user' is implemented"
            )
        home = home or Path.home()
        env = os.environ if env is None else env
        path = resolve_paths(home, env).settings

        document: dict = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text())
                if isinstance(loaded, dict):
                    document = loaded
            except (OSError, json.JSONDecodeError):
                document = {}
        hooks = document.get("hooks", {})
        if not isinstance(hooks, dict):
            hooks = {}

        found: dict[str, tuple[str, ...]] = {}
        for entry in HOOK_ENTRIES:
            # Legacy commands count as found. `remem hook session-end` runs
            # exactly what record-event runs, so the hook does fire; calling
            # it missing would send the user chasing a bug that is really a
            # rename waiting for the next install.
            ours = tuple(LEGACY_COMMANDS.get(entry.event, ())) + (entry.command,)
            commands: list[str] = []
            for group in hooks.get(entry.event, []) or []:
                if not isinstance(group, dict):
                    continue
                for hook in group.get("hooks", []) or []:
                    if isinstance(hook, dict) and hook.get("command") in ours:
                        commands.append(hook["command"])
            found[entry.event] = tuple(commands)

        return HookState(
            expected=tuple(h.expected() for h in HOOK_ENTRIES),
            path=path if path.exists() else None,
            found=found,
        )
```

Add `HookState` to the `remem.agents.base` imports and `import json` if not already present.

- [ ] **Step 4: Run and watch them pass**

Run: `uv run pytest tests/test_claude_code_hook_state.py -q`
Expected: PASS, 8 tests.

- [ ] **Step 5: Mutation-check the "not remem's business" guard**

Change `if isinstance(hook, dict) and hook.get("command") in ours:` to `if isinstance(hook, dict) and "command" in hook:`.
Expected: `test_another_tool_s_hook_on_the_same_event_is_not_remem_s_business` FAILS. Restore.

- [ ] **Step 6: Commit**

```bash
git add src/remem/agents/claude_code/adapter.py tests/test_claude_code_hook_state.py
git commit -m "Claude Code reports what its settings.json actually registers

Reads the file rather than jsonfile.read_json, which backs a corrupt file
up before returning empty - right for an install, wrong for a check that
must never write. A legacy command counts as found: the alias really does
fire, so calling it missing would send the user after the wrong bug."
```

---

### Task 3: `services/doctor.py` - the verdicts

**Files:**
- Create: `src/remem/services/doctor.py`
- Test: `tests/test_doctor_service.py` (create)

**Interfaces:**
- Consumes: `HookState`, `ExpectedHook` from Task 1; any adapter with `hook_state`.
- Produces: `Verdict` (StrEnum), `Finding`, `AgentReport`, `check(adapters, scope, home, env) -> list[AgentReport]`, `failed(reports) -> bool`, `render(reports) -> str`, `to_dict(reports) -> dict`, `advisories(reports) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_doctor_service.py`:

```python
"""Judging what an adapter found. Every policy decision lives here.

The rule this file is built around: never report ok for something that
was not checked. That is the failure claude-mem's opencode integration
shipped for months, and the one the extract jobs reproduced with
entries_written=0.
"""

from __future__ import annotations

from pathlib import Path

from remem.agents.base import ExpectedHook, HookState
from remem.services import doctor


def hooks(**found):
    expected = (
        ExpectedHook("SessionStart", "remem hook session-start", True, "injection"),
        ExpectedHook("PostToolUse", "remem hook record-event", True, "every tool call"),
        ExpectedHook("SessionEnd", "remem hook record-event", False, "prompt close"),
    )
    return HookState(
        expected=expected,
        path=Path("/tmp/settings.json"),
        found={h.event: tuple(found.get(h.event, ())) for h in expected},
    )


class FakeAdapter:
    name = "fake"

    def __init__(self, state=None, raises=False):
        self._state, self._raises = state, raises

    def hook_state(self, scope, home, env):
        if self._raises:
            raise RuntimeError("adapter is broken")
        return self._state


class NoCapability:
    name = "plain"


def verdicts(report):
    return {f.event: f.verdict for f in report.findings}


def test_a_registered_hook_is_ok():
    state = hooks(SessionStart=["remem hook session-start"],
                  PostToolUse=["remem hook record-event"],
                  SessionEnd=["remem hook record-event"])
    report = doctor.check({"fake": FakeAdapter(state)})[0]
    assert set(verdicts(report).values()) == {doctor.Verdict.OK}
    assert not doctor.failed([report])


def test_a_missing_required_hook_fails_the_check():
    state = hooks(SessionStart=["remem hook session-start"],
                  SessionEnd=["remem hook record-event"])
    report = doctor.check({"fake": FakeAdapter(state)})[0]
    assert verdicts(report)["PostToolUse"] is doctor.Verdict.MISSING
    assert doctor.failed([report])


def test_a_missing_optional_hook_is_reported_but_does_not_fail():
    state = hooks(SessionStart=["remem hook session-start"],
                  PostToolUse=["remem hook record-event"])
    report = doctor.check({"fake": FakeAdapter(state)})[0]
    assert verdicts(report)["SessionEnd"] is doctor.Verdict.MISSING
    assert not doctor.failed([report])


def test_a_hook_registered_twice_is_duplicated_and_does_not_fail():
    """It fires twice and doubles every row it records, which is worth
    saying loudly - but the hook does fire, so the exit code stays 0."""
    state = hooks(SessionStart=["remem hook session-start"],
                  PostToolUse=["remem hook record-event"] * 2)
    report = doctor.check({"fake": FakeAdapter(state)})[0]
    assert verdicts(report)["PostToolUse"] is doctor.Verdict.DUPLICATED
    assert not doctor.failed([report])


def test_a_superseded_command_is_stale_not_missing():
    state = hooks(SessionStart=["remem hook session-start"],
                  PostToolUse=["remem hook post-tool-use-old"])
    report = doctor.check({"fake": FakeAdapter(state)})[0]
    assert verdicts(report)["PostToolUse"] is doctor.Verdict.STALE
    assert not doctor.failed([report])


def test_an_adapter_with_no_remem_hooks_at_all_is_simply_not_installed():
    """Warning that Cursor's hooks are missing on a machine with no Cursor
    would make the whole report noise, and a report people skim hides the
    next PostToolUse."""
    report = doctor.check({"fake": FakeAdapter(hooks())})[0]
    assert report.installed is False
    assert report.findings == ()
    assert not doctor.failed([report])


def test_an_adapter_without_the_capability_is_unchecked_never_ok():
    report = doctor.check({"plain": NoCapability()})[0]
    assert report.verdict is doctor.Verdict.UNCHECKED
    assert report.findings == ()
    assert not doctor.failed([report])


def test_an_adapter_that_raises_warns_and_the_rest_still_run():
    """The registry contract: a broken third-party adapter must never be
    why remem will not run."""
    state = hooks(SessionStart=["remem hook session-start"],
                  PostToolUse=["remem hook record-event"],
                  SessionEnd=["remem hook record-event"])
    reports = doctor.check({
        "broken": FakeAdapter(raises=True),
        "fake": FakeAdapter(state),
    })
    broken = next(r for r in reports if r.agent == "broken")
    good = next(r for r in reports if r.agent == "fake")
    assert broken.verdict is doctor.Verdict.UNCHECKED
    assert "adapter is broken" in broken.warning
    assert set(verdicts(good).values()) == {doctor.Verdict.OK}


def test_unchecked_never_renders_as_ok():
    text = doctor.render(doctor.check({"plain": NoCapability()}))
    assert "ok" not in text.lower()
    assert "no hook registration to check" in text.lower()


def test_the_advisory_names_the_hook_and_the_fix():
    state = hooks(SessionStart=["remem hook session-start"],
                  SessionEnd=["remem hook record-event"])
    lines = doctor.advisories(doctor.check({"fake": FakeAdapter(state)}))
    assert len(lines) == 1
    assert "PostToolUse" in lines[0]
    assert "remem doctor fake" in lines[0]


def test_a_complete_install_produces_no_advisory():
    state = hooks(SessionStart=["remem hook session-start"],
                  PostToolUse=["remem hook record-event"],
                  SessionEnd=["remem hook record-event"])
    assert doctor.advisories(doctor.check({"fake": FakeAdapter(state)})) == []


def test_json_and_human_forms_read_off_the_same_reports():
    state = hooks(SessionStart=["remem hook session-start"])
    reports = doctor.check({"fake": FakeAdapter(state)})
    data = doctor.to_dict(reports)
    assert data["failed"] is True
    assert data["agents"][0]["hooks"][1]["verdict"] == "missing"
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_doctor_service.py -q`
Expected: FAIL with `ImportError: cannot import name 'doctor' from 'remem.services'`.

- [ ] **Step 3: Implement the service**

Create `src/remem/services/doctor.py`:

```python
"""Is what is on disk what this adapter installs?

The failure this answers: on 2026-08-30 Claude Code had recorded zero tool
calls for the life of the events pipeline, because settings.json held three
hooks and not four. Hooks are fail-soft and print nothing, `record status`
reports what was recorded and cannot know what should have been, and extract
jobs finished `done` with entries_written=0 - indistinguishable from a quiet
session. Nothing in remem could see it.

Adapters answer with facts (`HookState`); every judgement is made here, so
that all of them agree on what "missing" means and so one computation feeds
`remem doctor`, its --json form, and the advisory line in
`remem record status`.

Opens no database connection. `remem doctor` must work with Postgres down,
for the same reason `services/settings.py` does: a diagnostic that needs the
system to be healthy is no use when it is not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Mapping

from remem.agents.base import HookState


class Verdict(StrEnum):
    OK = "ok"
    MISSING = "missing"
    #: Registered more than once. It fires twice and doubles every row it
    #: records - what 525b491 detects downstream by inferring from duplicate
    #: event rows, visible here before a single duplicate row is written.
    DUPLICATED = "duplicated"
    #: A superseded command remains where the canonical one belongs. The
    #: hook still fires, because remem's legacy commands are real aliases,
    #: so this is a warning and not a failure.
    STALE = "stale"
    #: Not examined - the adapter has no hook configuration to check, or its
    #: implementation raised. Never rendered as `ok`; reporting success for
    #: something never verified is the exact failure this module exists to
    #: prevent.
    UNCHECKED = "unchecked"


@dataclass(frozen=True, slots=True)
class Finding:
    agent: str
    event: str
    verdict: Verdict
    required: bool
    provides: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AgentReport:
    agent: str
    #: The config file examined, or None - either because the adapter has no
    #: hook configuration or because the file does not exist.
    path: Path | None = None
    #: An adapter is INSTALLED when at least one remem command appears in
    #: its config. Zero is "not installed" and stays quiet; some-but-not-all
    #: is the bug this module exists to catch and is never quiet.
    installed: bool = False
    findings: tuple[Finding, ...] = ()
    #: Set only when the whole adapter could not be examined.
    verdict: Verdict | None = None
    warning: str | None = None


def _judge(agent: str, state: HookState) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for hook in state.expected:
        commands = tuple(state.found.get(hook.event, ()))
        if not commands:
            verdict, detail = Verdict.MISSING, ""
        elif len(commands) > 1:
            verdict = Verdict.DUPLICATED
            detail = f"registered {len(commands)} times"
        elif commands[0] != hook.command:
            verdict = Verdict.STALE
            detail = f"names `{commands[0]}`, superseded by `{hook.command}`"
        else:
            verdict, detail = Verdict.OK, ""
        findings.append(Finding(
            agent=agent, event=hook.event, verdict=verdict,
            required=hook.required, provides=hook.provides, detail=detail,
        ))
    return tuple(findings)


def check(
    adapters: Mapping[str, object],
    scope: str = "user",
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[AgentReport]:
    """One report per adapter, in name order.

    `hook_state` is probed with getattr rather than required, exactly as
    `event()`, `env_settings()`, `settings_path()` and `inject()` are: a
    third-party adapter written before this existed must keep working, and
    a broken one must never be why `remem doctor` will not run.
    """
    home = home or Path.home()
    env = dict(os.environ) if env is None else dict(env)
    reports: list[AgentReport] = []

    for name in sorted(adapters):
        adapter = adapters[name]
        adapter = adapter() if isinstance(adapter, type) else adapter
        state_fn = getattr(adapter, "hook_state", None)
        if state_fn is None:
            reports.append(AgentReport(agent=name, verdict=Verdict.UNCHECKED))
            continue
        try:
            state = state_fn(scope, home, env)
        except Exception as exc:
            reports.append(AgentReport(
                agent=name, verdict=Verdict.UNCHECKED,
                warning=f"could not read {name}'s hook configuration: {exc}",
            ))
            continue

        installed = any(state.found.get(h.event) for h in state.expected)
        reports.append(AgentReport(
            agent=name,
            path=state.path,
            installed=installed,
            findings=_judge(name, state) if installed else (),
        ))
    return reports


def failed(reports: list[AgentReport]) -> bool:
    """True when a required hook is missing on any adapter.

    Only MISSING-and-required fails. STALE and DUPLICATED both still fire
    the hook, and UNCHECKED reports the absence of a check rather than the
    presence of a fault - exiting non-zero for "I could not tell" would
    train users to ignore the exit code, and the exit code is the half a
    script reads.
    """
    return any(
        f.verdict is Verdict.MISSING and f.required
        for r in reports for f in r.findings
    )


def advisories(reports: list[AgentReport]) -> list[str]:
    """One line per adapter whose install is incomplete, for `record
    status` - which is what a user runs when a harness looks quiet, and is
    otherwise structurally incapable of answering."""
    lines: list[str] = []
    for report in reports:
        broken = [f for f in report.findings if f.verdict is not Verdict.OK]
        if not broken:
            continue
        named = ", ".join(f"{f.event} ({f.verdict})" for f in broken)
        lines.append(
            f"{report.agent} is installed but its hooks are incomplete: "
            f"{named} - run `remem doctor {report.agent}`"
        )
    return lines


def render(reports: list[AgentReport]) -> str:
    lines: list[str] = []
    for report in reports:
        if report.verdict is Verdict.UNCHECKED:
            lines.append(f"{report.agent}: no hook registration to check")
            if report.warning:
                lines.append(f"  {report.warning}")
            continue
        if not report.installed:
            lines.append(f"{report.agent}: not installed")
            continue
        lines.append(f"{report.agent}  {report.path}")
        for f in report.findings:
            mark = "ok" if f.verdict is Verdict.OK else f.verdict.upper()
            note = f"  - {f.detail or f.provides}" if f.verdict is not Verdict.OK else ""
            lines.append(f"  {f.event:<18} {mark}{note}")
        if any(f.verdict is not Verdict.OK for f in report.findings):
            lines.append(f"  Fix: remem install {report.agent}")
    return "\n".join(lines)


def to_dict(reports: list[AgentReport]) -> dict:
    """The --json form. A thin formatter over the same reports `render`
    reads, so the two can never disagree."""
    return {
        "failed": failed(reports),
        "agents": [
            {
                "agent": r.agent,
                "path": str(r.path) if r.path else None,
                "installed": r.installed,
                "verdict": str(r.verdict) if r.verdict else None,
                "warning": r.warning,
                "hooks": [
                    {
                        "event": f.event,
                        "verdict": str(f.verdict),
                        "required": f.required,
                        "provides": f.provides,
                        "detail": f.detail,
                    }
                    for f in r.findings
                ],
            }
            for r in reports
        ],
    }
```

- [ ] **Step 4: Run and watch them pass**

Run: `uv run pytest tests/test_doctor_service.py -q`
Expected: PASS, 12 tests.

- [ ] **Step 5: Mutation-check the two rulings that matter**

a) In `check`, change the `state_fn is None` branch to `AgentReport(agent=name, installed=True)`.
Expected: `test_an_adapter_without_the_capability_is_unchecked_never_ok` and `test_unchecked_never_renders_as_ok` FAIL. Restore.

b) In `failed`, drop `and f.required`.
Expected: `test_a_missing_optional_hook_is_reported_but_does_not_fail` FAILS. Restore.

- [ ] **Step 6: Commit**

```bash
git add src/remem/services/doctor.py tests/test_doctor_service.py
git commit -m "A service that judges what an adapter found on disk

Adapters answer with facts; every verdict is decided here, so all of them
agree on what missing means and one computation feeds the report, --json
and the record status advisory. Unchecked never renders as ok - reporting
success for something never verified is the failure this catches."
```

---

### Task 4: `remem doctor`

**Files:**
- Modify: `src/remem/cli.py`
- Test: `tests/test_doctor_cli.py` (create)

**Interfaces:**
- Consumes: `services.doctor.check/render/to_dict/failed`, `agents.registry.discover`.
- Produces: the `remem doctor` command.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_doctor_cli.py`. **No `pytest.mark.db`** - and that is the point of the last test.

```python
"""`remem doctor`. Reads files, opens no database."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from remem.cli import app

runner = CliRunner()


def settings_with(tmp_path, hooks):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}))
    return path


def entry(command):
    return {"matcher": "", "hooks": [{"type": "command", "command": command}]}


COMPLETE = {
    "SessionStart": [entry("remem hook session-start")],
    "SessionEnd": [entry("remem hook record-event")],
    "PostToolUse": [entry("remem hook record-event")],
    "UserPromptSubmit": [entry("remem hook session-size")],
}


def test_a_missing_required_hook_is_named_and_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    hooks = dict(COMPLETE)
    del hooks["PostToolUse"]
    settings_with(tmp_path, hooks)
    result = runner.invoke(app, ["doctor", "claude-code"])
    assert result.exit_code == 1
    assert "PostToolUse" in result.stdout
    assert "remem install claude-code" in result.stdout


def test_a_complete_install_exits_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    settings_with(tmp_path, COMPLETE)
    result = runner.invoke(app, ["doctor", "claude-code"])
    assert result.exit_code == 0
    assert "MISSING" not in result.stdout


def test_json_carries_the_same_verdict_and_exit_code(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    hooks = dict(COMPLETE)
    del hooks["PostToolUse"]
    settings_with(tmp_path, hooks)
    result = runner.invoke(app, ["doctor", "claude-code", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["failed"] is True


def test_an_unknown_agent_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(app, ["doctor", "no-such-harness"])
    assert result.exit_code == 1
    assert "no-such-harness" in result.stdout


def test_the_scope_flag_reaches_the_adapter(tmp_path, monkeypatch):
    """Claude Code implements only user scope and raises UnsupportedScope
    for anything else. That raise must land where the registry contract
    says it lands - unchecked with a warning, not a traceback."""
    monkeypatch.setenv("HOME", str(tmp_path))
    settings_with(tmp_path, COMPLETE)
    result = runner.invoke(app, ["doctor", "claude-code", "--scope", "project"])
    assert result.exit_code == 0
    assert "project" in result.stdout


def test_doctor_needs_no_database(tmp_path, monkeypatch):
    """The whole point of a diagnostic: it has to work when the system is
    unhealthy. services/settings.py is the existing precedent."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("REMEM_DSN", "postgresql://nobody@127.0.0.1:1/nothing")
    settings_with(tmp_path, COMPLETE)
    result = runner.invoke(app, ["doctor", "claude-code"])
    assert result.exit_code == 0
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_doctor_cli.py -q`
Expected: FAIL - `No such command 'doctor'`.

- [ ] **Step 3: Implement the command**

Add to `src/remem/cli.py`, beside the existing `verify` command:

```python
@app.command("doctor")
def doctor(
    agent: Annotated[Optional[str], typer.Argument()] = None,
    scope: Annotated[str, typer.Option("--scope")] = "user",
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Check that each harness's config registers the hooks remem installs.

    The gap this closes: hooks are fail-soft, so a harness that was never
    told to call remem looks exactly like one with nothing to say. `remem
    verify` proves remem records when called; this asks whether the harness
    will ever call it. Neither answers the other's question.

    Opens no database connection - a diagnostic that needs the system
    healthy is no use when it is not.
    """
    from remem.agents import registry
    from remem.services import doctor as doctor_service

    adapters = registry.discover()
    if agent is not None:
        if agent not in adapters:
            known = ", ".join(sorted(adapters)) or "none"
            typer.echo(f"unknown agent '{agent}'. Available: {known}")
            raise typer.Exit(1)
        adapters = {agent: adapters[agent]}

    reports = doctor_service.check(
        adapters, scope=scope, home=Path.home(), env=dict(os.environ)
    )
    if as_json:
        typer.echo(json.dumps(doctor_service.to_dict(reports), indent=2))
    else:
        typer.echo(doctor_service.render(reports))
    raise typer.Exit(1 if doctor_service.failed(reports) else 0)
```

- [ ] **Step 4: Run and watch them pass**

Run: `uv run pytest tests/test_doctor_cli.py -q`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add src/remem/cli.py tests/test_doctor_cli.py
git commit -m "remem doctor

Exits 1 only for a missing required hook. STALE and DUPLICATED still fire
the hook and UNCHECKED reports the absence of a check, so neither fails
the command - exiting non-zero for 'I could not tell' trains people to
ignore the exit code."
```

---

### Task 5: Cursor's `hook_state`

**Files:**
- Modify: `src/remem/agents/cursor/install.py:19-23` (`ENTRIES`)
- Modify: `src/remem/agents/cursor/adapter.py`
- Test: `tests/test_cursor_hook_state.py` (create)
- Test: `tests/test_cursor_install.py` (add one guard)

**Interfaces:**
- Consumes: `ExpectedHook`, `HookState` from Task 1.
- Produces: `remem.agents.cursor.install.HOOK_ENTRIES: tuple[CursorHook, ...]`, `ENTRIES` derived from it, `CursorAdapter.hook_state`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cursor_hook_state.py`:

```python
"""What .cursor/hooks.json actually registers."""

from __future__ import annotations

import json

from remem.agents.cursor.adapter import CursorAdapter

RECORD = "remem record event --agent cursor"
CONTEXT = "remem hook context --agent cursor"


def write_hooks(home, hooks):
    path = home / ".cursor" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "hooks": hooks}))
    return path


def test_a_missing_message_hook_is_reported(tmp_path):
    write_hooks(tmp_path, {
        "sessionStart": [{"command": CONTEXT}],
        "postToolUse": [{"command": RECORD}],
        "beforeSubmitPrompt": [{"command": RECORD}],
    })
    state = CursorAdapter().hook_state("user", tmp_path, {})
    assert state.found["afterAgentResponse"] == ()


def test_another_tool_s_cursor_hook_is_left_out_of_the_report(tmp_path):
    write_hooks(tmp_path, {
        "postToolUse": [{"command": "other-tool"}, {"command": RECORD}],
    })
    state = CursorAdapter().hook_state("user", tmp_path, {})
    assert state.found["postToolUse"] == (RECORD,)


def test_both_message_hooks_are_required(tmp_path):
    """Marked optional in the first draft, on the grounds that losing them
    costs extraction quality rather than recording. 8cb186c measured that
    distinction away: a fragment of a session returned nothing in three
    runs where the whole session returned entries in five of five."""
    state = CursorAdapter().hook_state("user", tmp_path, {})
    required = {h.event for h in state.expected if h.required}
    assert required == {
        "sessionStart", "postToolUse", "beforeSubmitPrompt", "afterAgentResponse",
    }


def test_no_hooks_file_is_an_answer_not_an_error(tmp_path):
    state = CursorAdapter().hook_state("user", tmp_path, {})
    assert state.path is None
    assert all(v == () for v in state.found.values())


def test_project_scope_reads_the_repository_s_own_hooks_file(tmp_path, monkeypatch):
    """Cursor is the one adapter with two real scopes, and hooks_path
    resolves project scope from cwd - so the check has to look where the
    install wrote, not where the user's home is."""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".cursor" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(
        {"version": 1, "hooks": {"postToolUse": [{"command": RECORD}]}}
    ))
    state = CursorAdapter().hook_state("project", tmp_path / "elsewhere", {})
    assert state.found["postToolUse"] == (RECORD,)
    assert state.path == path
```

Add to `tests/test_cursor_install.py` - literals, not the constant:

```python
def test_the_hook_table_names_exactly_what_install_writes():
    from remem.agents.cursor.install import HOOK_ENTRIES

    assert sorted(h.event for h in HOOK_ENTRIES) == sorted([
        "sessionStart", "postToolUse", "beforeSubmitPrompt", "afterAgentResponse",
    ])
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_cursor_hook_state.py tests/test_cursor_install.py -q -m "cursor or not cursor"`
Expected: FAIL - no `HOOK_ENTRIES`, no `hook_state`.

- [ ] **Step 3: Table in `cursor/install.py`**

Replace the `ENTRIES` dict with a table it is derived from, so `merge()` and every existing caller keep the mapping they expect:

```python
@dataclass(frozen=True, slots=True)
class CursorHook:
    event: str
    command: str
    required: bool
    provides: str

    def expected(self) -> ExpectedHook:
        return ExpectedHook(self.event, self.command, self.required, self.provides)


#: The one table. `install()` writes from ENTRIES below and `hook_state()`
#: checks against this, so they cannot disagree about which hooks exist.
#:
#: `remem record event --agent cursor`, NOT `remem hook record-event` -
#: that one is hardcoded to the Claude Code hook and takes no --agent, so
#: naming it here would record nothing, silently. A test pins this.
#:
#: Every name here must be in hooks.HOOK_NAMES and none may be in
#: hooks.BLOCKING_HOOKS; tests assert both.
#:
#: All four are required. The message hooks were optional in the first
#: draft of the doctor design, on the grounds that losing them costs
#: extraction quality rather than recording itself; the render-budget
#: measurement (8cb186c) removed that distinction. Showing the extractor a
#: fragment of a session returned nothing in three runs where the whole
#: session returned entries in five of five, and an install missing both
#: message hooks records tool calls with every prompt and answer cut out -
#: which is that fragment.
HOOK_ENTRIES: tuple[CursorHook, ...] = (
    CursorHook("sessionStart", "remem hook context --agent cursor", True,
               "context injection, and the only extraction trigger Cursor has"),
    CursorHook("postToolUse", "remem record event --agent cursor", True,
               "every tool call"),
    CursorHook("beforeSubmitPrompt", "remem record event --agent cursor", True,
               "the user's prompts, as extraction input"),
    CursorHook("afterAgentResponse", "remem record event --agent cursor", True,
               "the agent's responses, as extraction input"),
)

#: hook name to command, the shape `merge()` and `install()` take.
ENTRIES = {h.event: h.command for h in HOOK_ENTRIES}
```

Import `dataclass` and `ExpectedHook`.

- [ ] **Step 4: Implement `CursorAdapter.hook_state`**

```python
    def hook_state(
        self,
        scope: str = "user",
        home: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> HookState:
        """What hooks.json actually registers, against HOOK_ENTRIES.

        Read-only: unlike `merge()`, which backs the file up before
        rewriting it, this never writes. Unreadable JSON reads as no remem
        hooks - the honest answer, since the file names none that can be
        found.
        """
        from remem.agents.cursor.install import (
            HOOK_ENTRIES, LEGACY_COMMANDS, hooks_path,
        )

        home = home or Path.home()
        env = os.environ if env is None else env
        path = hooks_path(scope, home=home, cwd=Path.cwd())

        document: dict = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text())
                if isinstance(loaded, dict):
                    document = loaded
            except (OSError, json.JSONDecodeError):
                document = {}
        hooks = document.get("hooks", {})
        if not isinstance(hooks, dict):
            hooks = {}

        found: dict[str, tuple[str, ...]] = {}
        for entry in HOOK_ENTRIES:
            ours = tuple(LEGACY_COMMANDS.get(entry.event, ())) + (entry.command,)
            found[entry.event] = tuple(
                h["command"]
                for h in hooks.get(entry.event, []) or []
                if isinstance(h, dict) and h.get("command") in ours
            )

        return HookState(
            expected=tuple(h.expected() for h in HOOK_ENTRIES),
            path=path if path.exists() else None,
            found=found,
        )
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_cursor_hook_state.py tests/test_cursor_install.py tests/test_cursor_event.py -q -m "cursor or not cursor"`
Expected: PASS. The pre-existing install tests must be unchanged - `ENTRIES` still has the same shape and contents.

- [ ] **Step 6: Commit**

```bash
git add src/remem/agents/cursor/install.py src/remem/agents/cursor/adapter.py tests/test_cursor_hook_state.py tests/test_cursor_install.py
git commit -m "Cursor reports what its hooks.json registers

All four hooks are required. The two message hooks were optional in the
first draft; the render-budget measurement removed the distinction between
'costs recording' and 'costs extraction quality' - a fragment of a session
returned nothing in three runs where the whole session returned entries in
five of five, and an install missing both message hooks records exactly
that fragment."
```

---

### Task 6: The advisory line in `remem record status`

**Files:**
- Modify: `src/remem/services/events.py` (`StatusReport`, `status`, `render`, `to_dict`)
- Modify: `src/remem/cli.py` (`record_status`)
- Test: `tests/test_record_status.py`

**Interfaces:**
- Consumes: `services.doctor.check/advisories`.
- Produces: `StatusReport.hook_advisories: list[str]`; `events.status(..., hook_advisories=None)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_record_status.py`:

```python
def test_the_status_report_carries_hook_advisories(store, owner):
    """`record status` is what a user runs when a harness looks quiet, and
    is otherwise structurally incapable of answering - it reports what WAS
    recorded and cannot know what should have been. It is also the only
    place that can reach a harness which has recorded nothing ever and so
    appears nowhere in event_stats."""
    report = events.status(
        store, owner.id, idle_seconds=IDLE,
        hook_advisories=["claude-code is installed but its hooks are "
                         "incomplete: PostToolUse (missing) - run "
                         "`remem doctor claude-code`"],
    )
    text = events.render(report)
    assert "PostToolUse" in text
    assert "remem doctor claude-code" in text
    assert events.to_dict(report)["hook_advisories"] == report.hook_advisories


def test_a_healthy_install_adds_no_advisory_lines(store, owner):
    report = events.status(store, owner.id, idle_seconds=IDLE)
    assert report.hook_advisories == []
    assert "remem doctor" not in events.render(report)
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_record_status.py -q -k advisor`
Expected: FAIL - `status() got an unexpected keyword argument 'hook_advisories'`.

- [ ] **Step 3: Thread it through the service**

In `StatusReport`, add - and check the module's imports first, since
`StatusReport`'s existing list fields have no defaults and `field` may not
be imported yet:

```python
    #: Lines from `services/doctor.advisories`, passed in rather than
    #: computed here: this module is about the database and doctor is about
    #: the filesystem, and `status` must keep working when a config file
    #: cannot be read.
    hook_advisories: list[str] = field(default_factory=list)
```

In `status()`, add the parameter `hook_advisories: list[str] | None = None` and pass `hook_advisories=list(hook_advisories or [])` into the returned `StatusReport`.

In `render()`, after the per-harness lines:

```python
    for line in report.hook_advisories:
        # "!" and not "warning:" - this is the line that would have saved
        # an unknown number of unrecorded sessions, and it has to survive
        # being skimmed.
        lines.append(f"! {line}")
```

In `to_dict()`, add `"hook_advisories": list(report.hook_advisories),`.

- [ ] **Step 4: Wire the CLI**

In `record_status`, before opening the session:

```python
    from remem.agents import registry
    from remem.services import doctor as doctor_service

    # Wrapped: an unreadable config file must not take down a status
    # command that is otherwise about the database. doctor.check already
    # degrades per adapter; this covers the registry call itself.
    try:
        advisories = doctor_service.advisories(
            doctor_service.check(registry.discover(), env=dict(os.environ))
        )
    except Exception:
        advisories = []
```

and pass `hook_advisories=advisories` to `events.status(...)`.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_record_status.py -q -m "cursor or not cursor"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/remem/services/events.py src/remem/cli.py tests/test_record_status.py
git commit -m "record status names an incomplete hook install

The advisories are passed in, not computed in the events service: that
module is about the database and doctor is about the filesystem, and
status has to keep working when a config file cannot be read."
```

---

### Task 7: Documentation

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-09-01-hook-install-doctor-design.md` (status line)

- [ ] **Step 1: Add the section to CLAUDE.md**

Under "Events and extraction", after the invariants list:

```markdown
### Checking the install

`remem doctor` answers the one question a fail-soft pipeline cannot ask
itself: **does the installed config actually register the hooks this adapter
installs?** It reads files and opens no database - a diagnostic that needs
the system healthy is no use when it is not.

Each adapter answers with facts (`hook_state()`, a probed optional
capability returning `HookState`); `services/doctor.py` makes every
judgement, so all adapters agree on what "missing" means and one
computation feeds `remem doctor`, its `--json`, and one advisory line in
`remem record status`.

Three rules worth not breaking:

- **One table per adapter.** `HOOK_ENTRIES` is read by both `install()` and
  `hook_state()`. Two tables kept in step would drift, and drift is the
  whole bug: settings.json held three hooks for the life of the events
  pipeline and nothing could see it.
- **Unchecked never renders as `ok`.** opencode ships a plugin file rather
  than hook configuration, so it does not implement `hook_state()` and the
  report says "no hook registration to check". Reporting success for
  something never verified is the failure this command exists to catch.
- **Only a missing *required* hook exits non-zero.** `STALE` and
  `DUPLICATED` still fire the hook; `UNCHECKED` reports the absence of a
  check. Exiting non-zero for "I could not tell" trains people to ignore
  the exit code.

`doctor` and `verify` are a pair and neither subsumes the other: doctor asks
whether the harness will ever call remem, `verify` asks whether remem works
when called.
```

- [ ] **Step 2: Mark the spec implemented**

Change its `Status:` line to `implemented (2026-09-01)`.

- [ ] **Step 3: Full suite**

Run: `uv run pytest -m "cursor or not cursor" -q`
Expected: PASS, **and check the skip count is 0.** Baseline was 778 passed; this plan adds roughly 30 tests.

- [ ] **Step 4: Prove it against the real machine**

Run: `remem doctor`
Expected: a real report for claude-code and cursor, and "no hook registration to check" for opencode. This is the first time the command has run against a config nobody wrote for a test.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-09-01-hook-install-doctor-design.md
git commit -m "Document the install doctor"
```
