# Cursor Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the Cursor adapter - the third and last harness in the events-and-recall design - so Cursor records tool calls and both sides of the conversation, and receives the knowledge base block once per session.

**Architecture:** A new `src/remem/agents/cursor/` package registered under the `remem.agents` entry point group. It generates **no script of any kind**: `.cursor/hooks.json` names the `remem` CLI directly, because `remem record event --agent cursor` and `remem hook context --agent cursor` already read their payloads as JSON on stdin. Context injection becomes the fourth optional adapter capability, `inject()`, probed with `getattr` exactly as `event()`, `env_settings()` and `settings_path()` are - Cursor's writes `.cursor/rules/remem.mdc` and returns the path; Claude Code does not implement it and keeps printing to stdout.

**Tech Stack:** Python 3.14, Typer, pytest. No new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-08-29-cursor-adapter-design.md`

## Global Constraints

- Python 3.14. `from __future__ import annotations` at the top of every module.
- **Prose and comments use spaced hyphens ` - `, never em dashes.** This applies to code comments, docstrings, commit messages and the plan's own output.
- Comments explain *why*, at length, especially where a decision looks arbitrary. A subtle invariant with no comment reads as an accident to the next reader.
- **Hooks are fail-soft.** Every code path a hook can reach exits 0, prints nothing on error, and never raises. Reasons go to stderr under `REMEM_HOOK_DEBUG` via `remem.hookio.debug`.
- **Frontends parse and format; they never decide.** A policy branch belongs in `services/` or on the adapter, never in `cli.py`.
- Prefer failing loudly over quietly doing something else - `UnsupportedScope` is raised for an unknown scope, never a fallback.
- **Every write to a user-owned file backs it up first and says where the backup went.**
- The command the hooks call is `remem record event --agent cursor`, **not** `remem hook record-event` (which is hardcoded to Claude Code and takes no `--agent`).
- Cursor's hook names are Cursor's own (`postToolUse`, `beforeSubmitPrompt`, ...). Do **not** use Cursor's Claude Code compatibility layer.
- Tests that need Postgres are marked `@pytest.mark.db`. A green run with skips is not a green run - check the skip count.

---

### Task 1: Clean up `agents/verify.py`

Three minor findings from the opencode branch's final review, in the shared round-trip this adapter will call unchanged. Doing them first means Task 6 builds on correct code rather than inheriting a known wart.

**Files:**
- Modify: `src/remem/agents/verify.py`
- Test: `tests/test_verify_cleanup.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `round_trip(agent_name: str, env: Mapping[str, str] | None = None) -> InstallReport`, unchanged in signature. Task 6 calls it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_verify_cleanup.py`:

```python
"""A failed delete must not skip `disable`.

The reserved project is forced into recording by round_trip() and must
never be left that way. The cleanup block ran both statements
unguarded, so an exception from the delete skipped the disable and left
__remem_verify__ recording for the rest of the process's life.
"""

from __future__ import annotations

from remem.agents import verify


def test_a_failing_delete_still_disables_recording(monkeypatch):
    disabled: list[str] = []

    class FakeStore:
        def delete_session_events(self, *args):
            raise RuntimeError("delete blew up")

        def events_for_session(self, *args):
            return [object()]

    class FakeOwner:
        id = "owner-1"
        handle = "someone"

    class FakeSession:
        store = FakeStore()
        owner = FakeOwner()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake_record = type(
        "FakeRecord",
        (),
        {
            "enable": staticmethod(lambda store, owner, project: None),
            "disable": staticmethod(
                lambda store, owner, project: disabled.append(project)
            ),
            "record": staticmethod(
                lambda store, owner, event, agent: object()
            ),
        },
    )

    monkeypatch.setattr("remem.config.load", lambda env=None: object())
    monkeypatch.setattr(
        "remem.session.open_session", lambda config: FakeSession()
    )
    monkeypatch.setattr("remem.services.record", fake_record)

    report = verify.round_trip("cursor", env={})

    assert disabled == [verify.VERIFY_PROJECT], (
        "disable must run even when the delete raises"
    )
    assert any("delete blew up" in w for w in report.warnings)


def test_the_cleanup_comment_counts_every_exit():
    """The comment enumerated three exits when there are four."""
    source = (
        __import__("pathlib").Path(verify.__file__).read_text()
    )
    assert "could not record" in source and "could not read it back" in source
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_verify_cleanup.py -v`
Expected: FAIL - `disabled == []`, because the raised delete skips the disable.

- [ ] **Step 3: Guard the two cleanup statements independently**

In `src/remem/agents/verify.py`, replace the body of the `finally:` block. The existing comment stays and is corrected to name four exits, not three:

```python
            finally:
                # Runs on every path out of the block above - the happy
                # path, the "could not record" return, the "could not read
                # it back" return, and any exception - because the reserved
                # project must never be left recording, and the test event
                # it wrote must never be left behind, however the
                # round-trip went.
                #
                # Scoped to exactly this (owner, project, harness,
                # session) - never `services.events.prune`, whose
                # contract is a time window over every event this owner
                # has ever recorded, in every project, and which a
                # `force=True` call here would have deleted wholesale.
                #
                # The two statements are guarded separately, and that is
                # the point: they are independent obligations, and a
                # failure to delete one scratch row in a project nothing
                # reads must not be why recording is left enabled. The
                # disable is the more important of the two.
                try:
                    s.store.delete_session_events(
                        s.owner.id, VERIFY_PROJECT, agent_name, session_id
                    )
                except Exception as exc:
                    report.warnings.append(
                        "install verification could not delete its test "
                        f"event: {type(exc).__name__}: {exc}"
                    )
                try:
                    record_service.disable(s.store, s.owner.id, VERIFY_PROJECT)
                except Exception as exc:
                    report.warnings.append(
                        "install verification could not disable recording "
                        f"for '{VERIFY_PROJECT}': {type(exc).__name__}: {exc}"
                    )
```

- [ ] **Step 4: Drop the unused re-export lint**

`VERIFY_PROJECT` is re-exported from `src/remem/agents/claude_code/adapter.py` for compatibility and reads as an unused import. Make the re-export explicit rather than incidental - in `claude_code/adapter.py`, keep the import and add it to a module `__all__`:

```python
# Re-exported, not used here: tests/test_claude_code_events_install.py
# imports VERIFY_PROJECT from this module, and the name moved to
# agents/verify.py when the round-trip was lifted out of this adapter.
# Naming it in __all__ is what makes that deliberate rather than a stray
# import a linter should remove.
__all__ = ["ClaudeCodeAdapter", "VERIFY_PROJECT"]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_verify_cleanup.py tests/test_opencode_verify.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/remem/agents/verify.py src/remem/agents/claude_code/adapter.py tests/test_verify_cleanup.py
git commit -m "Guard verify's two cleanup obligations independently

A raised delete skipped the disable and left the reserved project
recording. They are independent obligations and the disable is the more
important one, so each gets its own try. Also corrects the comment,
which enumerated three exits when there are four, and makes the
VERIFY_PROJECT re-export explicit."
```

---

### Task 2: Vendor Cursor's hook names, with a freshness reader

**Files:**
- Create: `src/remem/agents/cursor/__init__.py`
- Create: `src/remem/agents/cursor/hooks.py`
- Test: `tests/test_cursor_hooks_contract.py`
- Modify: `pyproject.toml` (add the `cursor` marker)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `HOOK_NAMES: frozenset[str]` - all 21 names.
  - `CURSOR_VERSION: str` - `"3.9.16"`.
  - `installed_hook_names(app: Path) -> frozenset[str]` - reads the names out of a Cursor.app bundle; returns an empty frozenset if the bundle is not there. Tasks 4 and 5 both import `HOOK_NAMES`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cursor_hooks_contract.py`:

```python
"""Two questions, deliberately not one test - the same split the opencode
contract test makes, and for the same reason.

  1. Do we subscribe to something we believe is not real?
     (Always runs, everywhere, no Cursor installed.)
  2. Is what we believe still true?
     (Reads the installed Cursor.app, and may skip.)

Collapsing them produces a guard that skips on CI, which is the failure
the `db` markers already taught this project to distrust.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remem.agents.cursor.hooks import (
    CURSOR_VERSION,
    HOOK_NAMES,
    installed_hook_names,
)

CURSOR_APP = Path("/Applications/Cursor.app")


def test_the_vendored_list_holds_the_hooks_we_subscribe_to():
    """A floor, not the whole list: HOOK_NAMES is every hook Cursor has,
    and these four are the ones this adapter uses."""
    assert {
        "sessionStart",
        "postToolUse",
        "beforeSubmitPrompt",
        "afterAgentResponse",
    } <= HOOK_NAMES


def test_the_vendored_list_has_every_name_cursor_enumerates():
    assert len(HOOK_NAMES) == 21


def test_the_vendored_list_records_where_it_came_from():
    assert CURSOR_VERSION == "3.9.16"


def test_the_blocking_hooks_are_not_subscribed():
    """Six hooks make Cursor wait for a permission decision. Subscribing
    to one would put a fail-soft hook in front of the user's tool calls."""
    from remem.agents.cursor.hooks import BLOCKING_HOOKS

    assert BLOCKING_HOOKS <= HOOK_NAMES
    assert "preToolUse" in BLOCKING_HOOKS
    assert "postToolUse" not in BLOCKING_HOOKS


def test_parsing_a_bundle(tmp_path):
    """The reader, exercised without needing Cursor installed."""
    bundle = tmp_path / "Contents" / "Resources" / "app" / "out" / "vs" / "workbench"
    bundle.mkdir(parents=True)
    (bundle / "workbench.desktop.main.js").write_text(
        'x={beforeShellExecution:"beforeShellExecution",'
        'postToolUse:"postToolUse",sessionStart:"sessionStart",'
        'workspaceOpen:"workspaceOpen"}}}),other={notAHook:"notAHook"}'
    )

    names = installed_hook_names(tmp_path)

    assert names == {
        "beforeShellExecution",
        "postToolUse",
        "sessionStart",
        "workspaceOpen",
    }
    assert "notAHook" not in names


def test_reading_a_bundle_that_is_not_there(tmp_path):
    assert installed_hook_names(tmp_path / "nope") == frozenset()


@pytest.mark.cursor
def test_the_vendored_list_still_matches_the_installed_app():
    """Guards our copy of an external fact, not the adapter.

    This one may skip - what it protects is the freshness of HOOK_NAMES,
    and only a machine with Cursor installed can answer it. Cursor
    auto-updates, so expect it to fire.
    """
    if not CURSOR_APP.exists():
        pytest.skip(
            "Cursor.app is not installed; nothing to compare the vendored "
            "hook list against"
        )

    installed = installed_hook_names(CURSOR_APP)

    assert installed == HOOK_NAMES, (
        "Cursor's hook set has changed. Update HOOK_NAMES and "
        "CURSOR_VERSION in src/remem/agents/cursor/hooks.py, then check "
        "whether the adapter should subscribe to anything new."
    )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cursor_hooks_contract.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'remem.agents.cursor'`.

- [ ] **Step 3: Write the module**

Create `src/remem/agents/cursor/__init__.py` as an empty file.

Create `src/remem/agents/cursor/hooks.py`:

```python
"""Every hook name Cursor emits, vendored.

This file is a copy of an external fact, checked in on purpose - the same
bargain `agents/opencode/hooks.py` makes, and for the same reason. Reading
Cursor.app at test time makes the contract test skip wherever Cursor is not
installed, which is everywhere that matters, CI included. A guard that
skips is not a guard.

So the checked-in list is what the adapter is tested against, and a
separate `cursor`-marked test asserts this list still matches the installed
app. The two answer different questions; see
tests/test_cursor_hooks_contract.py.

Transcribed from Cursor 3.9.16, by reading the hook enumeration in
Contents/Resources/app/out/vs/workbench/workbench.desktop.main.js.
"""

from __future__ import annotations

import re
from pathlib import Path

CURSOR_VERSION = "3.9.16"

HOOK_NAMES = frozenset(
    {
        "beforeShellExecution",
        "beforeMCPExecution",
        "afterShellExecution",
        "afterMCPExecution",
        "beforeReadFile",
        "afterFileEdit",
        "beforeTabFileRead",
        "afterTabFileEdit",
        "stop",
        "beforeSubmitPrompt",
        "afterAgentResponse",
        "afterAgentThought",
        "sessionStart",
        "sessionEnd",
        "preCompact",
        "subagentStart",
        "subagentStop",
        "preToolUse",
        "postToolUse",
        "postToolUseFailure",
        "workspaceOpen",
    }
)

#: The hooks Cursor WAITS on - it reads a permission decision from their
#: stdout and will not proceed until they answer. This adapter subscribes to
#: none of them, and that is what keeps the fail-soft contract meaningful
#: here: a hook that cannot deny anything cannot deny anything by failing.
#: Checked in rather than merely documented so a future edit that reaches
#: for one of these fails a test instead of shipping.
BLOCKING_HOOKS = frozenset(
    {
        "beforeShellExecution",
        "beforeMCPExecution",
        "beforeReadFile",
        "beforeTabFileRead",
        "subagentStart",
        "preToolUse",
    }
)

#: Cursor's bundle, minified, enumerates its hooks as an object of
#: identical key/value string pairs: `sessionStart:"sessionStart",`. That
#: self-naming shape is what makes a crude reader viable - it is specific
#: enough that ordinary minified code does not match it by accident.
#:
#: Deliberately crude, exactly as the opencode types parser is: this reads
#: one known file to refresh one checked-in list, and anything more
#: principled would be a dependency out of all proportion to the job. It
#: will break on a bundler change - which is the correct behaviour for a
#: freshness check, and the always-runs half of the contract test does not
#: depend on it.
_PAIR = re.compile(r"\b([a-z][A-Za-z]*)\s*:\s*\"\1\"")

#: Where Cursor keeps the bundle inside the .app.
_BUNDLE = Path("Contents/Resources/app/out/vs/workbench/workbench.desktop.main.js")


def installed_hook_names(app: Path) -> frozenset[str]:
    """Read the hook names out of an installed Cursor.app.

    `app` is the .app directory. Returns an empty set if the bundle is not
    there; the caller decides whether that is a skip or a failure, because
    the answer differs between the two tests that call this.
    """
    bundle = app / _BUNDLE
    if not bundle.exists():
        return frozenset()

    text = bundle.read_text(errors="replace")
    # The enumeration is one object literal. Anchor on a name we know is in
    # it and take a window around it, rather than matching the pattern
    # across a 30MB file where an unrelated self-naming pair could sneak in.
    anchor = text.find('beforeSubmitPrompt:"beforeSubmitPrompt"')
    if anchor == -1:
        return frozenset()
    window = text[max(0, anchor - 2000) : anchor + 2000]
    return frozenset(_PAIR.findall(window))
```

- [ ] **Step 4: Register the pytest marker**

In `pyproject.toml`, add to the `markers` list:

```toml
    "cursor: requires Cursor.app installed; checks our vendored copy is fresh",
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cursor_hooks_contract.py -v`
Expected: PASS. On this machine the `cursor`-marked test runs (Cursor.app 3.9.16 is installed) rather than skipping - if it fails, the reader's window is wrong, not the vendored list.

- [ ] **Step 6: Commit**

```bash
git add src/remem/agents/cursor/ tests/test_cursor_hooks_contract.py pyproject.toml
git commit -m "Vendor Cursor's 21 hook names and a freshness reader

Split in two the same way the opencode contract test is: the always-runs
half needs nothing installed, the cursor-marked half checks the vendored
copy against Cursor.app. BLOCKING_HOOKS is checked in rather than
documented so reaching for one fails a test."
```

---

### Task 3: Probe a real Cursor payload

**This task is a spike, and it is the only task with an external dependency.** The spec cannot specify Cursor's payload field names - which key carries the session id, which the workspace root - because they are not readable from minified JS with any confidence. Tasks 4 and 5 are written against what this task finds.

**Files:**
- Create: `docs/superpowers/notes/2026-08-29-cursor-payloads.md`
- Modify: none. **No production code in this task.**

**Interfaces:**
- Consumes: `HOOK_NAMES` from Task 2, to know what to subscribe to.
- Produces: a documented payload shape per hook. Task 4's `identity()` and `event()` are written from it.

- [ ] **Step 1: Ask the user before running the network installer**

The `cursor-agent` CLI on this machine is `2025.09.12`, which predates hooks. Updating it runs a third-party network installer. **Stop and ask.** Do not run it unprompted.

If the user declines the update, fall back to driving Cursor.app by hand: the capture hook below works identically, it just needs a human to open the app and take a turn.

- [ ] **Step 2: Write a capture hook into a scratch workspace**

```bash
mkdir -p /tmp/cursor-probe/.cursor
cd /tmp/cursor-probe && git init -q
cat > /tmp/cursor-probe/capture.sh <<'EOF'
#!/bin/sh
# Appends one line of JSON per invocation: the hook name and its raw payload.
printf '{"hook":"%s","payload":%s}\n' "$1" "$(cat)" >> /tmp/cursor-probe/payloads.jsonl
EOF
chmod +x /tmp/cursor-probe/capture.sh
cat > /tmp/cursor-probe/.cursor/hooks.json <<'EOF'
{
  "version": 1,
  "hooks": {
    "sessionStart": [{"command": "/tmp/cursor-probe/capture.sh sessionStart"}],
    "postToolUse": [{"command": "/tmp/cursor-probe/capture.sh postToolUse"}],
    "beforeSubmitPrompt": [{"command": "/tmp/cursor-probe/capture.sh beforeSubmitPrompt"}],
    "afterAgentResponse": [{"command": "/tmp/cursor-probe/capture.sh afterAgentResponse"}]
  }
}
EOF
```

- [ ] **Step 3: Drive a real session and capture**

With an updated CLI: `cd /tmp/cursor-probe && cursor-agent -p "list the files here, then tell me what you see"` - a prompt that forces at least one tool call, so `postToolUse` fires.

By hand: open `/tmp/cursor-probe` in Cursor.app and take the same turn.

- [ ] **Step 4: Record the findings**

Write `docs/superpowers/notes/2026-08-29-cursor-payloads.md` with, for each of the four hooks, the **exact** raw payload captured, and then a table stating:

| question | key |
|---|---|
| session id | ? |
| workspace root | ? |
| tool name (postToolUse) | ? |
| whether `sessionStart` fires at all | ? |

If a hook did not fire, say so explicitly - that is a finding, not a gap, and Task 4 must not be written as though it did.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/notes/2026-08-29-cursor-payloads.md
git commit -m "Record Cursor's real hook payload shapes

Captured from a real session, because the field names are not readable
from the minified bundle. identity() and event() are written from this."
```

---

### Task 4: The adapter's `identity()` and `event()`

**Files:**
- Create: `src/remem/agents/cursor/adapter.py`
- Test: `tests/test_cursor_event.py`
- Modify: `pyproject.toml` (register the entry point)

**Interfaces:**
- Consumes: `HOOK_NAMES` from Task 2; the payload key names from Task 3's note. **Substitute the real key names for `SESSION_KEY` and `ROOT_KEY` below** - the code here uses named constants precisely so there is exactly one line to change per key.
- Produces:
  - `CursorAdapter.name = "cursor"`
  - `CursorAdapter.EVENT_KINDS: dict[str, EventKind]`
  - `CursorAdapter.identity(env, payload) -> Identity`
  - `CursorAdapter.event(env, payload) -> HarnessEvent | None`

  Tasks 5 and 6 add `inject()`, `install()` and `verify()` to this same class.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cursor_event.py`:

```python
"""The seam between Cursor's payload and the adapter, which nothing
type-checks.

A rename on either side means a harness that records nothing, silently,
while every other test stays green. This file is what stands between a
rename and that outcome - the counterpart to tests/test_opencode_event.py.
"""

from __future__ import annotations

from remem.agents.cursor.adapter import ROOT_KEY, SESSION_KEY, CursorAdapter
from remem.domain import EventKind


def _payload(hook: str, tmp_path, **extra) -> dict:
    return {
        "hook": hook,
        SESSION_KEY: "sess-1",
        ROOT_KEY: str(tmp_path),
        **extra,
    }


def test_a_tool_call_becomes_a_tool_call_event(tmp_path):
    adapter = CursorAdapter()

    event = adapter.event({}, _payload("postToolUse", tmp_path, tool_name="Shell"))

    assert event is not None
    assert event.kind is EventKind.TOOL_CALL
    assert event.session_id == "sess-1"
    assert event.tool == "Shell"


def test_both_message_hooks_become_message_events(tmp_path):
    adapter = CursorAdapter()

    for hook in ("beforeSubmitPrompt", "afterAgentResponse"):
        event = adapter.event({}, _payload(hook, tmp_path))
        assert event is not None, hook
        assert event.kind is EventKind.MESSAGE, hook


def test_session_start_is_not_an_event(tmp_path):
    """It injects rather than records, so it must never reach event() -
    exactly as opencode's system.transform never does."""
    adapter = CursorAdapter()

    assert adapter.event({}, _payload("sessionStart", tmp_path)) is None


def test_an_unsubscribed_hook_is_not_an_event(tmp_path):
    adapter = CursorAdapter()

    assert adapter.event({}, _payload("afterAgentThought", tmp_path)) is None
    assert adapter.event({}, _payload("beforeReadFile", tmp_path)) is None


def test_a_payload_with_no_session_id_is_not_an_event(tmp_path):
    """Without one the event cannot be grouped, and extraction is per
    session."""
    adapter = CursorAdapter()
    payload = _payload("postToolUse", tmp_path)
    del payload[SESSION_KEY]

    assert adapter.event({}, payload) is None


def test_the_payload_is_passed_through_whole(tmp_path):
    """The extractor is the half of this pipeline meant to be re-runnable
    without re-recording, so an adapter that pruned fields here would cap
    what any future extractor could ever see."""
    adapter = CursorAdapter()
    payload = _payload("postToolUse", tmp_path, weird_field={"nested": [1, 2]})

    event = adapter.event({}, payload)

    assert event is not None
    assert event.payload == payload


def test_identity_resolves_the_project_from_the_workspace_root(tmp_path):
    (tmp_path / ".git").mkdir()
    adapter = CursorAdapter()

    identity = adapter.identity({}, _payload("postToolUse", tmp_path))

    assert identity.agent == "cursor"
    assert identity.session_id == "sess-1"
    assert identity.project == tmp_path.name


def test_identity_of_an_empty_payload_is_not_an_error():
    """Every caller is a fail-soft hook."""
    adapter = CursorAdapter()

    identity = adapter.identity({}, {})

    assert identity.agent == "cursor"
    assert identity.session_id is None
    assert identity.project is None


def test_the_adapter_records_only_hooks_cursor_emits():
    from remem.agents.cursor.hooks import BLOCKING_HOOKS, HOOK_NAMES

    subscribed = frozenset(CursorAdapter.EVENT_KINDS)
    assert subscribed <= HOOK_NAMES
    assert not (subscribed & BLOCKING_HOOKS), (
        "a fail-soft hook must never sit where Cursor waits for a decision"
    )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cursor_event.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'remem.agents.cursor.adapter'`.

- [ ] **Step 3: Write the adapter**

Create `src/remem/agents/cursor/adapter.py`. **Set `SESSION_KEY` and `ROOT_KEY` to the real names from Task 3's note.**

```python
"""Installs remem into Cursor: hook entries merged into hooks.json, and a
generated rules file.

The contrast with the other two adapters is the point. opencode gets one
file remem owns outright; Claude Code gets entries merged into two JSON
files it does not own. Cursor gets both halves - `hooks.json` is
user-owned and shared, so it is merged and backed up, while the generated
`.mdc` is machine-owned and overwritten, because remem is the only thing
that ever writes it.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from remem.agents.base import HarnessEvent, Identity
from remem.domain import EventKind
from remem.project import resolve_project

#: The payload keys Cursor uses, named once so the seam has exactly one
#: line to change per key. Read off real payloads - see
#: docs/superpowers/notes/2026-08-29-cursor-payloads.md - because they are
#: not readable from the minified bundle with any confidence, and
#: tests/test_cursor_event.py is what stands between a rename here and a
#: harness that silently records nothing.
SESSION_KEY = "conversation_id"
ROOT_KEY = "workspace_roots"


class CursorAdapter:
    name = "cursor"

    #: Cursor hooks this adapter records, mapped to event kinds. Anything
    #: not in this table returns None.
    #:
    #: `sessionStart` is deliberately absent: it injects rather than
    #: records, so it never reaches event() - exactly as opencode's
    #: experimental.chat.system.transform is absent from its table.
    #:
    #: `postToolUse` rather than the three specific after* hooks: one
    #: parser instead of three, and no gap opens when Cursor adds a tool
    #: type. `afterAgentThought` is omitted because reasoning text is
    #: high-volume and low-signal for extraction. Every hook here is one
    #: Cursor does NOT wait on - see hooks.BLOCKING_HOOKS.
    EVENT_KINDS = {
        "postToolUse": EventKind.TOOL_CALL,
        "beforeSubmitPrompt": EventKind.MESSAGE,
        "afterAgentResponse": EventKind.MESSAGE,
    }

    def identity(self, env: Mapping[str, str], payload: dict) -> Identity:
        return Identity(
            agent=self.name,
            session_id=payload.get(SESSION_KEY),
            # The repository's name, not the directory's - resolving
            # through the git common directory files a subdirectory and a
            # worktree under the repository they belong to, the same as
            # every other write path in remem.
            project=self._project(payload),
        )

    def _project(self, payload: dict) -> str | None:
        root = payload.get(ROOT_KEY)
        # Cursor can hand back a list of workspace roots. The first is the
        # one the session was started in, which is the one whose knowledge
        # base the user means; a multi-root workspace picking a second
        # project's rules would be worse than picking the obvious one.
        if isinstance(root, list):
            root = root[0] if root else None
        if not root:
            return None
        return resolve_project(Path(root))

    def event(self, env: Mapping[str, str], payload: dict) -> HarnessEvent | None:
        """Read one Cursor hook payload as an event, or None.

        The payload is passed through WHOLE, for the same reason the other
        adapters do it: the extractor is the half of this pipeline meant to
        be fixable and re-runnable without re-recording anything, and an
        adapter that pruned fields here would cap what any future extractor
        could ever see.
        """
        kind = self.EVENT_KINDS.get(payload.get("hook", ""))
        if kind is None:
            return None
        identity = self.identity(env, payload)
        if not identity.session_id:
            # Without a session id the event cannot be grouped, and
            # extraction is triggered per session by idleness. Recording it
            # would be storage with no reader.
            return None
        return HarnessEvent(
            kind=kind,
            session_id=identity.session_id,
            project=identity.project,
            tool=payload.get("tool_name"),
            payload=payload,
            occurred_at=datetime.now(timezone.utc),
        )
```

- [ ] **Step 4: Register the entry point**

In `pyproject.toml`, under `[project.entry-points."remem.agents"]`:

```toml
cursor = "remem.agents.cursor.adapter:CursorAdapter"
```

Then re-install so the entry point is picked up: `uv tool install --editable .`

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cursor_event.py tests/test_cursor_hooks_contract.py -v`
Expected: PASS.

Then confirm the registry sees it: `remem record event --agent cursor --strict < /dev/null` should report unreadable stdin rather than "unknown agent 'cursor'".

- [ ] **Step 6: Commit**

```bash
git add src/remem/agents/cursor/adapter.py tests/test_cursor_event.py pyproject.toml
git commit -m "Read Cursor hook payloads as events

postToolUse for tool calls, beforeSubmitPrompt and afterAgentResponse for
both sides of the conversation - the raw material the stale spec said
Cursor could not give us. sessionStart injects rather than records and so
is absent from the table. Payload passed through whole."
```

---

### Task 5: The rules file and the `inject()` capability

**Files:**
- Create: `src/remem/agents/cursor/rules.py`
- Modify: `src/remem/agents/cursor/adapter.py` (add `inject`)
- Modify: `src/remem/agents/base.py` (document the capability on the Protocol)
- Modify: `src/remem/cli.py:808-878` (`hook_context` probes `inject`)
- Test: `tests/test_cursor_rules.py`

**Interfaces:**
- Consumes: `CursorAdapter.identity` from Task 4.
- Produces:
  - `rules.render(block: str) -> str`
  - `rules.write(root: Path, block: str) -> Path`
  - `rules.exclude(root: Path) -> bool` - True if it added the line, False if already present or there is no repository.
  - `CursorAdapter.inject(block: str, payload: dict) -> str | None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cursor_rules.py`:

```python
"""The rules file is the first time remem writes context into the user's
working tree rather than into a stream, so the policy around it is tested,
not assumed."""

from __future__ import annotations

from pathlib import Path

from remem.agents.cursor import rules
from remem.agents.cursor.adapter import ROOT_KEY, CursorAdapter

EXCLUDE_LINE = ".cursor/rules/remem.mdc"


def test_the_rendered_file_always_applies():
    out = rules.render("some knowledge")

    assert out.startswith("---\n")
    assert "alwaysApply: true" in out
    assert "some knowledge" in out


def test_the_rendered_file_says_it_is_generated():
    assert "generated by remem" in rules.render("x")


def test_writing_creates_the_rules_directory(tmp_path):
    path = rules.write(tmp_path, "hello")

    assert path == tmp_path / ".cursor" / "rules" / "remem.mdc"
    assert "hello" in path.read_text()


def test_writing_overwrites_unconditionally(tmp_path):
    """Machine-owned, like opencode's remem.js. A user who wants local
    edits here is asking for the wrong file."""
    rules.write(tmp_path, "first")

    path = rules.write(tmp_path, "second")

    assert "first" not in path.read_text()
    assert "second" in path.read_text()


def test_exclude_appends_to_git_info_exclude(tmp_path):
    (tmp_path / ".git" / "info").mkdir(parents=True)

    added = rules.exclude(tmp_path)

    assert added is True
    assert EXCLUDE_LINE in (tmp_path / ".git" / "info" / "exclude").read_text()


def test_exclude_is_idempotent(tmp_path):
    (tmp_path / ".git" / "info").mkdir(parents=True)
    rules.exclude(tmp_path)

    added = rules.exclude(tmp_path)

    assert added is False
    text = (tmp_path / ".git" / "info" / "exclude").read_text()
    assert text.count(EXCLUDE_LINE) == 1


def test_exclude_preserves_what_is_already_there(tmp_path):
    info = tmp_path / ".git" / "info"
    info.mkdir(parents=True)
    (info / "exclude").write_text("# existing\n*.log\n")

    rules.exclude(tmp_path)

    text = (info / "exclude").read_text()
    assert "*.log" in text and EXCLUDE_LINE in text


def test_exclude_outside_a_repository_is_not_an_error(tmp_path):
    """A workspace outside a repository is an ordinary thing, not a
    failure."""
    assert rules.exclude(tmp_path) is False


def test_inject_writes_the_block_and_returns_the_path(tmp_path):
    adapter = CursorAdapter()

    path = adapter.inject("the block", {ROOT_KEY: str(tmp_path)})

    assert path == str(tmp_path / ".cursor" / "rules" / "remem.mdc")
    assert "the block" in Path(path).read_text()


def test_inject_without_a_root_writes_nothing(tmp_path):
    adapter = CursorAdapter()

    assert adapter.inject("the block", {}) is None


def test_inject_of_an_empty_block_writes_nothing(tmp_path):
    """An empty block means no knowledge base matched. Writing an empty
    rules file would hand Cursor a rule that says nothing, every session."""
    adapter = CursorAdapter()

    assert adapter.inject("", {ROOT_KEY: str(tmp_path)}) is None
    assert not (tmp_path / ".cursor").exists()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cursor_rules.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'remem.agents.cursor.rules'`.

- [ ] **Step 3: Write `rules.py`**

Create `src/remem/agents/cursor/rules.py`:

```python
"""The generated Cursor rules file, and the git hygiene around it.

Cursor cannot be handed context on stdout - its hookSpecificOutput
compatibility covers PreToolUse permission decisions only, with no
additionalContext path - so a file in the workspace is the only way in.
That makes this the first place remem writes context into the user's
working tree rather than into a stream, which is why the git half of this
module exists at all.
"""

from __future__ import annotations

from pathlib import Path

#: Relative to the workspace root. Also the line written to
#: .git/info/exclude, which is why it is spelled with forward slashes.
RULES_PATH = ".cursor/rules/remem.mdc"

#: alwaysApply is what makes Cursor include the rule in every chat in the
#: workspace without the user naming it. The description is what Cursor
#: shows in its rules list.
_FRONTMATTER = """\
---
description: remem knowledge base
alwaysApply: true
---

<!-- generated by remem - do not edit. Rewritten at every session start. -->

"""


def render(block: str) -> str:
    """The .mdc file's full text for one context block."""
    return _FRONTMATTER + block


def write(root: Path, block: str) -> Path:
    """Write the rules file under `root`, overwriting unconditionally.

    Machine-owned, like opencode's remem.js: no merge, no version marker,
    no prompt. A user who wants local edits to it is asking for the wrong
    file.
    """
    path = root / RULES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(block))
    return path


def exclude(root: Path) -> bool:
    """Add the rules file to .git/info/exclude. True if it added the line.

    .git/info/exclude rather than .gitignore, deliberately. .gitignore is
    tracked, reviewed and merged - appending to it hands the user a diff
    they did not ask for, and in a shared repository that lands in
    somebody's pull request. .git/info/exclude is local-only, needs no
    commit, and is exactly the mechanism git provides for "ignore this
    here, not for everyone".

    What this prevents is the .mdc being committed, which matters because
    its content is one user's knowledge base rendered at one moment:
    publishing it publishes personal notes, and it is stale the moment the
    knowledge base changes.

    Returns False, not an error, when there is no repository - a workspace
    outside one is an ordinary thing.
    """
    info = root / ".git" / "info"
    if not info.parent.is_dir():
        return False
    info.mkdir(parents=True, exist_ok=True)
    target = info / "exclude"

    existing = target.read_text() if target.exists() else ""
    if RULES_PATH in existing.split():
        return False

    prefix = "" if existing == "" or existing.endswith("\n") else "\n"
    target.write_text(f"{existing}{prefix}{RULES_PATH}\n")
    return True
```

- [ ] **Step 4: Add `inject()` to the adapter**

Append to `CursorAdapter` in `src/remem/agents/cursor/adapter.py`:

```python
    def inject(self, block: str, payload: dict) -> str | None:
        """Write the context block where Cursor will read it.

        The optional injection capability, probed with getattr exactly as
        event() is - Claude Code does not implement it, because stdout is
        the frontend's job there. Returning the path is what lets the CLI
        report where the block went without knowing what it wrote.

        Never raises: every caller is a fail-soft hook.
        """
        from remem.agents.cursor import rules

        if not block:
            # No knowledge base matched. Writing an empty rules file would
            # hand Cursor a rule that says nothing, every session.
            return None
        root = self._root(payload)
        if root is None:
            return None
        path = rules.write(root, block)
        rules.exclude(root)
        return str(path)

    def _root(self, payload: dict) -> Path | None:
        """The workspace root as a path, or None. Distinct from
        `_project`, which turns the same value into a knowledge base slug -
        injection needs the directory, recording needs the name."""
        root = payload.get(ROOT_KEY)
        if isinstance(root, list):
            root = root[0] if root else None
        return Path(root) if root else None
```

Then refactor `_project` to use `_root`, so the list-vs-string handling lives in one place:

```python
    def _project(self, payload: dict) -> str | None:
        root = self._root(payload)
        return resolve_project(root) if root else None
```

- [ ] **Step 5: Document the capability on the Protocol**

In `src/remem/agents/base.py`, inside the optional-capabilities comment block in `AgentAdapter`, after the `event()` paragraph:

```python
    #
    #     def inject(self, block: str, payload: dict) -> str | None: ...
    #
    # `inject()` is the delivery half of context injection, and it exists
    # because the three harnesses disagree about it completely: Claude Code
    # reads stdout, opencode returns a string from a plugin transform, and
    # Cursor reads a file in the workspace. services/context.py builds the
    # block for all of them; this says where it goes. An adapter without it
    # is one whose frontend already knows how to deliver the block - Claude
    # Code does not implement it - so a missing inject() is not a
    # degradation, it is the default. Returns the path or destination
    # written, for the frontend to report, or None if there was nowhere to
    # put it. Same degradation contract as the rest: a capability that
    # raises warns and continues.
```

- [ ] **Step 6: Probe `inject()` in the CLI**

In `src/remem/cli.py`, in `hook_context`, replace the `typer.echo(...)` block with:

```python
        cfg = load()
        with open_session(cfg) as s:
            rendered = context.block(
                s.store,
                s.owner.id,
                identity.project,
                cfg.max_chars,
                note=lambda reason: debug(env, reason),
                owner_handle=s.owner.handle,
            )

        # Delivery is the adapter's business, not the frontend's. An
        # adapter with no inject() is one whose harness reads stdout, which
        # is the default and not a degradation - see the Protocol comment
        # in agents/base.py. A capability that raises degrades to the
        # stdout path and a debug line, never to a broken hook.
        inject = getattr(adapter, "inject", None)
        if inject is not None:
            try:
                written = inject(rendered, payload)
            except Exception as exc:
                debug(
                    env,
                    f"{agent} adapter's inject() raised "
                    f"{type(exc).__name__}: {exc}",
                )
            else:
                debug(env, f"wrote the context block to {written}"
                      if written else "the adapter wrote no context block")
                raise typer.Exit(0)

        typer.echo(rendered, nl=False)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cursor_rules.py tests/test_cursor_event.py -v`
Expected: PASS.

Then check the Claude Code and opencode paths did not change:
Run: `uv run pytest tests/ -k "context or hook" -v`
Expected: PASS, no new failures.

- [ ] **Step 8: Commit**

```bash
git add src/remem/agents/cursor/rules.py src/remem/agents/cursor/adapter.py src/remem/agents/base.py src/remem/cli.py tests/test_cursor_rules.py
git commit -m "Make context delivery an adapter capability, and write Cursor's rules file

Cursor cannot be handed context on stdout, so inject() becomes the fourth
optional capability probed with getattr. Claude Code does not implement it
and keeps printing - a missing inject() is the default, not a degradation.

The .mdc goes in .git/info/exclude rather than .gitignore: .gitignore is
tracked and merged, and appending to it hands the user a diff they did not
ask for."
```

---

### Task 6: `install()`, the hooks.json merge, and `verify()`

**Files:**
- Create: `src/remem/agents/cursor/install.py`
- Modify: `src/remem/agents/cursor/adapter.py` (add `install`, `verify`)
- Test: `tests/test_cursor_install.py`

**Interfaces:**
- Consumes: `round_trip` from Task 1; `CursorAdapter` from Tasks 4-5.
- Produces:
  - `install.hooks_path(scope: str, home: Path, cwd: Path) -> Path`
  - `install.merge(path: Path, entries: dict[str, str]) -> tuple[dict, Path | None]` - returns the merged document and the backup path (None if the file did not exist).
  - `CursorAdapter.install(scope, home, env) -> InstallReport`
  - `CursorAdapter.verify(env, home) -> InstallReport`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cursor_install.py`:

```python
from __future__ import annotations

import json

import pytest

from remem.agents.base import UnsupportedScope
from remem.agents.cursor import install


def test_user_scope_is_the_home_cursor_directory(tmp_path):
    path = install.hooks_path("user", home=tmp_path, cwd=tmp_path / "repo")

    assert path == tmp_path / ".cursor" / "hooks.json"


def test_project_scope_is_the_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    path = install.hooks_path("project", home=tmp_path, cwd=repo)

    assert path == repo / ".cursor" / "hooks.json"


def test_an_unknown_scope_raises_rather_than_falling_back(tmp_path):
    with pytest.raises(UnsupportedScope):
        install.hooks_path("global", home=tmp_path, cwd=tmp_path)


def test_merging_into_nothing_creates_the_document(tmp_path):
    path = tmp_path / "hooks.json"

    merged, backup = install.merge(path, {"sessionStart": "remem hook context"})

    assert backup is None
    assert merged["version"] == 1
    assert merged["hooks"]["sessionStart"] == [{"command": "remem hook context"}]


def test_merging_preserves_another_tools_hooks(tmp_path):
    """hooks.json is user-owned and shared - unlike opencode's remem.js,
    which remem is the only thing that ever writes."""
    path = tmp_path / "hooks.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "stop": [{"command": "someone-elses-tool"}],
                    "sessionStart": [{"command": "also-theirs"}],
                },
            }
        )
    )

    merged, backup = install.merge(path, {"sessionStart": "remem hook context"})

    assert merged["hooks"]["stop"] == [{"command": "someone-elses-tool"}]
    commands = [h["command"] for h in merged["hooks"]["sessionStart"]]
    assert "also-theirs" in commands
    assert "remem hook context" in commands
    assert backup is not None and backup.exists()


def test_merging_twice_does_not_duplicate_our_entry(tmp_path):
    path = tmp_path / "hooks.json"
    entries = {"sessionStart": "remem hook context"}

    merged, _ = install.merge(path, entries)
    path.write_text(json.dumps(merged))
    merged, _ = install.merge(path, entries)

    assert len(merged["hooks"]["sessionStart"]) == 1


def test_the_backup_holds_what_was_there_before(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text('{"version": 1, "hooks": {"stop": [{"command": "x"}]}}')

    _, backup = install.merge(path, {"sessionStart": "remem hook context"})

    assert json.loads(backup.read_text())["hooks"]["stop"] == [{"command": "x"}]


def test_unreadable_json_is_backed_up_and_replaced(tmp_path):
    """A corrupt hooks.json must not stop the install, but the user's bytes
    must survive - which is exactly what the backup is for."""
    path = tmp_path / "hooks.json"
    path.write_text("{not json at all")

    merged, backup = install.merge(path, {"sessionStart": "remem hook context"})

    assert merged["hooks"]["sessionStart"] == [{"command": "remem hook context"}]
    assert backup is not None
    assert backup.read_text() == "{not json at all"


def test_the_installed_entries_name_only_hooks_cursor_emits():
    from remem.agents.cursor.hooks import BLOCKING_HOOKS, HOOK_NAMES

    named = frozenset(install.ENTRIES)

    assert named <= HOOK_NAMES, (
        f"install would subscribe to hooks Cursor does not emit: "
        f"{sorted(named - HOOK_NAMES)}"
    )
    assert not (named & BLOCKING_HOOKS)


def test_the_entries_call_the_harness_neutral_command():
    """`remem hook record-event` is hardcoded to Claude Code and takes no
    --agent. Getting this wrong records nothing, silently."""
    for hook, command in install.ENTRIES.items():
        assert "--agent cursor" in command
        assert "hook record-event" not in command


@pytest.mark.db
def test_install_writes_the_hooks_and_verifies(tmp_path, monkeypatch):
    """install() performs a live database round-trip - it proves the
    record path actually works - which is why this is marked db."""
    from remem.agents.cursor.adapter import CursorAdapter

    monkeypatch.chdir(tmp_path)
    report = CursorAdapter().install(scope="user", home=tmp_path, env=None)

    written = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())
    assert set(written["hooks"]) == set(install.ENTRIES)
    assert any("round-trip" in a for a in report.actions), report.warnings
    assert any("Recording is OFF" in n for n in report.notes)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cursor_install.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'remem.agents.cursor.install'`.

- [ ] **Step 3: Write `install.py`**

Create `src/remem/agents/cursor/install.py`:

```python
"""Where Cursor's hooks.json lives, and how remem merges into it."""

from __future__ import annotations

import json
import time
from pathlib import Path

from remem.agents.base import UnsupportedScope

#: The hook entries remem installs, hook name to command.
#:
#: `remem record event --agent cursor`, NOT `remem hook record-event` -
#: that one is hardcoded to the Claude Code hook and takes no --agent, so
#: naming it here would record nothing, silently. A test pins this.
#:
#: Every name here must be in hooks.HOOK_NAMES and none may be in
#: hooks.BLOCKING_HOOKS; tests assert both.
ENTRIES = {
    "sessionStart": "remem hook context --agent cursor",
    "postToolUse": "remem record event --agent cursor",
    "beforeSubmitPrompt": "remem record event --agent cursor",
    "afterAgentResponse": "remem record event --agent cursor",
}


def hooks_path(scope: str, home: Path, cwd: Path) -> Path:
    """The hooks.json for the given scope.

    Both scopes are real, unlike the Claude Code adapter's - Cursor reads
    a user file and a project file, and a per-repository tool has an
    obvious use for the second. An unknown scope raises rather than
    falling back: an install that reports success while having done
    something else is worse than one that refuses.
    """
    if scope == "user":
        return home / ".cursor" / "hooks.json"
    if scope == "project":
        return cwd / ".cursor" / "hooks.json"
    raise UnsupportedScope(
        f"scope '{scope}' is not supported; cursor supports 'user' or 'project'"
    )


def merge(path: Path, entries: dict[str, str]) -> tuple[dict, Path | None]:
    """Merge `entries` into the hooks.json at `path`.

    Returns the merged document and the backup path, or None if there was
    no file to back up.

    Merged rather than overwritten, unlike opencode's remem.js: hooks.json
    is user-owned and other tools legitimately write to it, so remem adds
    its entries beside theirs and never removes one it did not put there.

    Unreadable JSON is treated as an empty document - but only after the
    original bytes are safely in the backup. A corrupt hooks.json must not
    stop the install, and it must not cost the user what they had.
    """
    backup: Path | None = None
    document: dict = {}

    if path.exists():
        raw = path.read_text()
        backup = path.with_suffix(f".json.bak{int(time.time())}")
        backup.write_text(raw)
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                document = loaded
        except json.JSONDecodeError:
            document = {}

    document["version"] = document.get("version", 1)
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        document["hooks"] = hooks

    for hook, command in entries.items():
        group = hooks.setdefault(hook, [])
        if not isinstance(group, list):
            group = []
            hooks[hook] = group
        # Idempotent by command string: re-running the install must not
        # grow the file, and a hook that fires twice per event would
        # double every row it records.
        if not any(
            isinstance(h, dict) and h.get("command") == command for h in group
        ):
            group.append({"command": command})

    return document, backup
```

- [ ] **Step 4: Add `install()` and `verify()` to the adapter**

Append to `CursorAdapter` in `src/remem/agents/cursor/adapter.py`, and add the imports `json`, `RECORD_INSTALL_REPORT` pieces:

```python
    def install(
        self,
        scope: str = "user",
        home: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> InstallReport:
        """Merge remem's hook entries into hooks.json, then verify.

        Writes nothing else. The rules file is deliberately NOT written
        here: it is written at sessionStart, from the workspace the session
        actually opened, which is what makes one user-scope install work
        for every repository.
        """
        from remem.agents.cursor.install import ENTRIES, hooks_path, merge

        home = home or Path.home()
        env = os.environ if env is None else env

        # hooks_path raises for an unknown scope, so by the time anything
        # is written the scope is known good.
        path = hooks_path(scope, home=home, cwd=Path.cwd())
        path.parent.mkdir(parents=True, exist_ok=True)
        document, backup = merge(path, ENTRIES)
        path.write_text(json.dumps(document, indent=2) + "\n")

        report = InstallReport(agent=self.name)
        report.actions.append(
            f"Merged {len(ENTRIES)} hook entries into {path}"
        )
        if backup is not None:
            # A .bak nobody is told about is barely a safety net.
            report.actions.append(f"Backed up the previous file to {backup}")
        report.notes.append(RECORD_NOTE)
        report.notes.append(
            "The knowledge base block is written to .cursor/rules/remem.mdc "
            "at every session start, and added to .git/info/exclude so it "
            "stays out of git."
        )

        # Verification last, deliberately, and never raising - a failure
        # here becomes a warning folded into this same report, exactly as
        # both other adapters do it.
        verification = self.verify(env=env, home=home)
        report.actions.extend(verification.actions)
        report.warnings.extend(verification.warnings)
        return report

    def verify(
        self, env: Mapping[str, str] | None = None, home: Path | None = None
    ) -> InstallReport:
        """Record an event, read it back, delete it.

        `home` is accepted for symmetry with install() but unused: this is
        a database round-trip, not a file-system one. What it proves and
        why it is shaped this way lives in agents/verify.round_trip,
        shared with every adapter.
        """
        from remem.agents.verify import round_trip

        return round_trip(self.name, env)
```

Add to the imports at the top of `adapter.py`:

```python
import json

from remem.agents.base import (
    RECORD_NOTE,
    HarnessEvent,
    Identity,
    InstallReport,
)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cursor_install.py -v`
Expected: PASS. Check the skip count - the `db` test must run, not skip. If it skips, start Postgres with `docker compose up -d`.

- [ ] **Step 6: Confirm the CLI end of it**

Run: `remem install cursor --scope user`

Remember the agent argument is **positional** - `remem install --agent cursor` fails with a no-such-option error.

Expected: it reports the merge, the backup if there was one, the round-trip, and the recording note.

- [ ] **Step 7: Commit**

```bash
git add src/remem/agents/cursor/install.py src/remem/agents/cursor/adapter.py tests/test_cursor_install.py
git commit -m "Install remem's hooks into Cursor's hooks.json

Merged, not overwritten: hooks.json is user-owned and other tools write
there too, so remem adds beside them, backs up first, and says where the
backup went. Idempotent by command string - a hook that fired twice would
double every row it records."
```

---

### Task 7: Prove it end to end, and document it

Contract tests are not the bar. opencode was proven against the real harness and Cursor gets the same treatment.

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md` (an adapter section, alongside the opencode one)
- Create: `docs/superpowers/notes/2026-08-29-cursor-proof.md`
- Modify: `docs/superpowers/specs/2026-08-29-cursor-adapter-design.md` (Status: implemented)

**Interfaces:**
- Consumes: everything from Tasks 1-6.
- Produces: no code. Evidence and documentation.

- [ ] **Step 1: Run the full suite**

Run: `uv run pytest`
Expected: PASS. **Check the skip count** - a green run with the `db` tests skipped is not a green run.

- [ ] **Step 2: Install into a scratch project and enable recording**

```bash
mkdir -p /tmp/cursor-proof && cd /tmp/cursor-proof && git init -q
remem install cursor --scope project
remem kb new cursor-proof
remem remember --project cursor-proof --kind rule --title "Proof marker" \
  --body "The string PROOF-MARKER-7 must appear in any injected block."
remem record enable --project cursor-proof
```

- [ ] **Step 3: Drive a real session**

`cd /tmp/cursor-proof && cursor-agent -p "list the files here and summarise them"`, or the same turn by hand in Cursor.app if the CLI was not updated in Task 3.

- [ ] **Step 4: Confirm all four claims**

Each needs evidence, not inference:

1. **Events landed, both kinds.**
   `remem events show --project cursor-proof` - expect at least one `tool_call` and one `message`.
2. **The rules file was written.**
   `cat /tmp/cursor-proof/.cursor/rules/remem.mdc` - expect the frontmatter and `PROOF-MARKER-7`.
3. **The block reached the model.** Confirm from what was **sent**, not from what came back - a model can mention a marker it inferred. Ask the session a question only the block answers, or read the request log if the configured model server keeps one.
4. **Written once per session, not once per turn.** Take two turns in one session and check the file's mtime does not advance on the second.

- [ ] **Step 5: Confirm the git hygiene**

```bash
cd /tmp/cursor-proof && git status --short
grep -n "cursor/rules/remem.mdc" .git/info/exclude
```

Expected: the `.mdc` does not appear in `git status`, and the line is in `.git/info/exclude`. `.gitignore` must be untouched.

- [ ] **Step 6: Write the evidence up**

`docs/superpowers/notes/2026-08-29-cursor-proof.md`: the exact commands run, the Cursor version, and the output proving each of the four claims. If a claim could not be proven, say which and why - an unproven claim recorded as unproven is worth far more than a claim quietly dropped.

- [ ] **Step 7: Document the adapter**

In `CLAUDE.md`, add a `### The cursor adapter` section after the opencode one, covering: the four hooks and why not the others; `.cursor/hooks.json` merged while the `.mdc` is overwritten; `.git/info/exclude` rather than `.gitignore`; `inject()` as the fourth probed capability; the two-part contract test; and that **a Cursor-only install records but never extracts**, the same gap the opencode adapter has.

In `README.md`, add Cursor to the install instructions, noting the positional agent argument.

- [ ] **Step 8: Mark the spec implemented**

In `docs/superpowers/specs/2026-08-29-cursor-adapter-design.md`, change `Status: proposed` to `Status: implemented`.

- [ ] **Step 9: Run the full suite one more time and commit**

```bash
uv run pytest
git add README.md CLAUDE.md docs/superpowers/
git commit -m "Prove the Cursor adapter against a real session, and document it

Four claims, each with evidence: events of both kinds recorded, the rules
file written, the block confirmed in what the model was sent, and written
once per session rather than once per turn."
```

---

## Self-Review

**Spec coverage.** Every section maps to a task: the vendored hook list and two-part contract test to Task 2; payload field names to Task 3; `identity`/`event` and the hook-to-kind table to Task 4; the rules file, `.git/info/exclude` and the `inject()` capability to Task 5; scope, the `hooks.json` merge, backups and `verify()` to Task 6; the end-to-end proof and the four claims to Task 7. The spec's out-of-scope items stay out. The `verify.py` cleanups are Task 1, which the spec does not cover because they are not design - flagged to the user before the plan was written.

**Placeholders.** None. Task 3 is a probe with unknown output by design, and that unknown is named explicitly rather than left as a TODO: Task 4 uses `SESSION_KEY` and `ROOT_KEY` constants so the substitution is one line each, and the plan says so at the task's Interfaces block.

**Type consistency.** `HOOK_NAMES` and `BLOCKING_HOOKS` (Task 2) are consumed by Tasks 4 and 6 under those names. `SESSION_KEY`/`ROOT_KEY` (Task 4) are imported by the tests in Tasks 4 and 5. `rules.render`/`write`/`exclude` (Task 5) are used only within Task 5. `install.ENTRIES`/`hooks_path`/`merge` (Task 6) are consumed by Task 6's own tests. `round_trip(agent_name, env)` keeps the signature Task 1 leaves it with. `RULES_PATH` is defined once in `rules.py` and is the same string written to `.git/info/exclude`.

**One risk worth naming.** Task 3 gates Tasks 4 and 5 on an external unknown. If Cursor's payloads turn out not to carry a stable session id at all, `event()` returns `None` for everything and the adapter records nothing - which the `db` install test in Task 6 would catch, but only after two tasks were written. If Task 3 finds no session id, stop and re-open the design rather than working around it.
