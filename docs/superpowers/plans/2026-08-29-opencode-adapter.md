# opencode Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a second harness adapter so remem records events from and injects
knowledge into opencode, through a plugin that depends on nothing.

**Architecture:** A generated, import-free JavaScript plugin marshals three
opencode hooks into JSON and shells out to `remem`; every policy decision stays
in Python, in `OpenCodeAdapter` and the services beneath it. The knowledge base
is injected once per session, held by a `Set` in the plugin because per-process
state has nowhere else to live.

**Tech Stack:** Python 3.14, Typer, hatchling, pytest. JavaScript (ES module,
run by Bun inside opencode). `@opencode-ai/plugin` 1.3.5 as a types-only
reference, never a runtime dependency.

**Spec:** `docs/superpowers/specs/2026-08-29-opencode-adapter-design.md`

## Global Constraints

- Python 3.14. `from __future__ import annotations` at the top of every module.
- In prose, comments and docs: spaced hyphens ` - `, never em dashes.
- Comments explain *why*, at length, wherever a decision looks arbitrary. Match
  the density of the surrounding code.
- Frontends parse and format; they never decide. A policy branch belongs in
  `services/`, never in `cli.py`.
- Nothing outside `session.py` / `backends/` imports psycopg.
- The adapter implements **neither** `env_settings()` nor `settings_path()`.
  They are a pair; an adapter with one of them is worse than an adapter with
  neither.
- The plugin's `plugin.js` **imports nothing**. `@opencode-ai/plugin` may appear
  in test fixtures and comments, never in a runtime `import`.
- Plugin timeouts: **5000ms** for event recording, **10000ms** for the knowledge
  base block. These match the 5s and 10s the Claude Code hooks are registered
  with.
- Hook names this adapter subscribes to, verbatim, and no others:
  `tool.execute.after`, `chat.message`, `experimental.chat.system.transform`.
- Vendored types reference version: `@opencode-ai/plugin` **1.3.5**.
- Every task ends green: `uv run pytest` with a **zero skip count** while
  Postgres is up (`docker compose up -d`, port 5433). A green run with skips is
  not a green run.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/remem/agents/opencode/__init__.py` | Empty, package marker. |
| `src/remem/agents/opencode/hooks.py` | The vendored list of hook names from `Hooks` in 1.3.5. Data only, no imports from the rest of remem. |
| `src/remem/agents/opencode/adapter.py` | `OpenCodeAdapter`: `install()`, `identity()`, `event()`, `verify()`. |
| `src/remem/agents/opencode/install.py` | Resolves opencode's plugin directory and writes `plugin.js`. |
| `src/remem/agents/opencode/plugin.js` | The marshaller. Package data, shipped in the wheel, overwritten into the user's plugin directory. |
| `src/remem/services/context.py` | Builds the knowledge base context block for a project. New home for `handoff_pointer`. |
| `src/remem/agents/verify.py` | The live install round-trip, shared by both adapters. |
| `src/remem/agents/claude_code/hook.py` | Modified: `session_start` delegates to `services/context.py`; `handoff_pointer` moves out. |
| `src/remem/cli.py` | Modified: `remem hook context --agent`, and `--agent` on `install`. |
| `pyproject.toml` | Modified: the `opencode` entry point. |
| `tests/test_opencode_event.py` | `event()` and `identity()` parsing. |
| `tests/test_opencode_hooks_contract.py` | Both halves of the contract test. |
| `tests/test_opencode_install.py` | Plugin file placement and overwrite. |
| `tests/test_context_service.py` | The extracted context block service. |
| `tests/test_opencode_verify.py` | The shared round-trip, `db`-marked. |

---

### Task 1: The adapter, and reading an opencode payload as an event

**Files:**
- Create: `src/remem/agents/opencode/__init__.py`
- Create: `src/remem/agents/opencode/adapter.py`
- Modify: `pyproject.toml:46-47`
- Test: `tests/test_opencode_event.py`

**Interfaces:**
- Consumes: `remem.agents.base.HarnessEvent`, `Identity`, `InstallReport`,
  `UnsupportedScope`; `remem.domain.EventKind`; `remem.project.resolve_project`.
- Produces: `OpenCodeAdapter` with `name = "opencode"`,
  `EVENT_KINDS: dict[str, EventKind]`,
  `identity(env: Mapping[str, str], payload: dict) -> Identity`, and
  `event(env: Mapping[str, str], payload: dict) -> HarnessEvent | None`.
  Later tasks add `install()` (Task 5) and `verify()` (Task 6) to this class.
- Produces: **the payload contract** that `plugin.js` must emit in Task 4:
  `{"hook": str, "sessionID": str, "cwd": str, ...}`, where `...` is whatever
  else the hook was handed.

**Context you need:** Read `src/remem/agents/claude_code/adapter.py` lines
104-113 (`EVENT_KINDS`) and 225-284 (`identity`, `event`). This task is the same
shape with a different key set. `remem record event --agent opencode` already
exists and is harness-neutral - `src/remem/cli.py:1078` - so **no CLI work is
needed for recording**. It probes `event()` with `getattr` and treats both
`None` and a raised exception as "cannot record", silently.

- [ ] **Step 1: Write the failing test**

Create `tests/test_opencode_event.py`:

```python
"""Reading opencode plugin payloads as events.

The payload shape here is the contract with plugin.js: change one and the
other stops recording, silently, because `remem record event` is fail-soft
by design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remem.agents.opencode.adapter import OpenCodeAdapter
from remem.domain import EventKind


@pytest.fixture
def repo(tmp_path):
    """A real git repository, so resolve_project has something to resolve."""
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def test_tool_call_becomes_a_tool_call_event(repo):
    payload = {
        "hook": "tool.execute.after",
        "sessionID": "ses_abc123",
        "cwd": str(repo),
        "tool": "bash",
        "callID": "call_1",
        "args": {"command": "ls"},
        "result": {"title": "ls", "output": "README.md", "metadata": {}},
    }

    event = OpenCodeAdapter().event({}, payload)

    assert event is not None
    assert event.kind is EventKind.TOOL_CALL
    assert event.session_id == "ses_abc123"
    assert event.tool == "bash"
    assert event.project == Path(repo).name


def test_chat_message_becomes_a_message_event(repo):
    payload = {
        "hook": "chat.message",
        "sessionID": "ses_abc123",
        "cwd": str(repo),
        "message": {"id": "msg_1", "role": "user"},
        "parts": [{"type": "text", "text": "hello"}],
    }

    event = OpenCodeAdapter().event({}, payload)

    assert event is not None
    assert event.kind is EventKind.MESSAGE
    assert event.tool is None


def test_the_whole_payload_is_carried_through_unparsed(repo):
    """The extractor is the half of the pipeline meant to be re-runnable.

    An adapter that picked fields out here would decide, at record time and
    forever, what a future extractor is allowed to see.
    """
    payload = {
        "hook": "tool.execute.after",
        "sessionID": "ses_abc123",
        "cwd": str(repo),
        "tool": "bash",
        "surprise": {"nested": [1, 2, 3]},
    }

    event = OpenCodeAdapter().event({}, payload)

    assert event.payload["surprise"] == {"nested": [1, 2, 3]}


def test_an_unsubscribed_hook_records_nothing(repo):
    payload = {"hook": "chat.params", "sessionID": "ses_abc123", "cwd": str(repo)}

    assert OpenCodeAdapter().event({}, payload) is None


def test_a_payload_with_no_session_id_records_nothing(repo):
    """Without a session id an event cannot be grouped for extraction, so
    recording it would be storage with no reader."""
    payload = {"hook": "tool.execute.after", "cwd": str(repo), "tool": "bash"}

    assert OpenCodeAdapter().event({}, payload) is None


def test_the_adapter_is_discoverable_under_its_entry_point():
    """Registry discovery reads *installed* entry points, so this test fails
    until the package is reinstalled - see the task's note on `uv sync`."""
    from remem.agents import registry

    assert registry.get("opencode") is OpenCodeAdapter


def test_the_adapter_declares_no_env_capabilities():
    """env_settings() and settings_path() are a pair, and remem has no
    curated table of opencode variables. An adapter with one of them cannot
    be written to but looks as though it can."""
    adapter = OpenCodeAdapter()

    assert not hasattr(adapter, "env_settings")
    assert not hasattr(adapter, "settings_path")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_opencode_event.py -v`
Expected: FAIL, collection error - `ModuleNotFoundError: No module named 'remem.agents.opencode'`

- [ ] **Step 3: Create the package and the adapter**

Create `src/remem/agents/opencode/__init__.py` as an empty file.

Create `src/remem/agents/opencode/adapter.py`:

```python
"""Installs remem into opencode: one generated plugin file, and nothing else.

The contrast with the Claude Code adapter is the point. That one writes an
MCP registration, four hook entries and a skills tree into two JSON files it
does not own. This one drops a single file into a directory opencode scans,
which means there is no user configuration to merge, to back up, or to
corrupt.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from remem.agents.base import HarnessEvent, Identity
from remem.domain import EventKind
from remem.project import resolve_project


class OpenCodeAdapter:
    name = "opencode"

    #: opencode plugin hooks this adapter records, mapped to event kinds.
    #: Anything not in this table returns None. The keys must stay a subset
    #: of remem.agents.opencode.hooks.HOOK_NAMES - subscribing to a hook
    #: opencode does not emit is the silent dead loop this whole design
    #: exists to prevent, and tests/test_opencode_hooks_contract.py is what
    #: makes that loud.
    #:
    #: experimental.chat.system.transform is deliberately absent: it injects
    #: rather than records, so it never reaches event().
    EVENT_KINDS = {
        "tool.execute.after": EventKind.TOOL_CALL,
        "chat.message": EventKind.MESSAGE,
    }

    def identity(self, env: Mapping[str, str], payload: dict) -> Identity:
        cwd = payload.get("cwd")
        return Identity(
            agent=self.name,
            session_id=payload.get("sessionID"),
            # The repository's name, not the directory's. opencode hands the
            # plugin both `directory` and `worktree`; whichever the plugin
            # sent, resolving through the git common directory files a
            # subdirectory and a worktree under the repository they belong
            # to, the same as every other write path in remem.
            project=resolve_project(Path(cwd)) if cwd else None,
        )

    def event(self, env: Mapping[str, str], payload: dict) -> HarnessEvent | None:
        """Read one opencode plugin payload as an event, or None.

        The payload is passed through WHOLE, for the same reason the Claude
        Code adapter does it: the extractor is the half of this pipeline
        meant to be fixable and re-runnable without re-recording anything,
        and an adapter that pruned fields here would cap what any future
        extractor could ever see.
        """
        kind = self.EVENT_KINDS.get(payload.get("hook", ""))
        if kind is None:
            return None
        identity = self.identity(env, payload)
        if not identity.session_id:
            # Without a session id the event cannot be grouped for
            # extraction, so recording it would be storage with no reader.
            return None
        return HarnessEvent(
            kind=kind,
            session_id=identity.session_id,
            # None for chat.message, which carries no tool. The column is
            # nullable precisely so a non-tool event does not have to invent
            # a value.
            tool=payload.get("tool"),
            project=identity.project,
            payload=payload,
            occurred_at=datetime.now(timezone.utc),
        )
```

- [ ] **Step 4: Register the entry point**

In `pyproject.toml`, extend the `remem.agents` group so it reads:

```toml
[project.entry-points."remem.agents"]
claude-code = "remem.agents.claude_code.adapter:ClaudeCodeAdapter"
opencode = "remem.agents.opencode.adapter:OpenCodeAdapter"
```

- [ ] **Step 5: Reinstall, or discovery will not see it**

Run: `uv sync && uv tool install --editable . --force`

`registry.discover()` reads entry points from **installed** distribution
metadata, not from `pyproject.toml`. Editing the file alone changes nothing:
`remem record event --agent opencode` will exit 0 having recorded nothing,
because `UnknownAgent` is caught and debug-logged. This is the single most
likely way to lose an hour on this task.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_opencode_event.py -v`
Expected: PASS, 7 passed

- [ ] **Step 7: Run the whole suite**

Run: `docker compose up -d && uv run pytest`
Expected: PASS, and the skip count is **0**. If it is not zero, Postgres is
down and the run proved less than it claims.

- [ ] **Step 8: Commit**

```bash
git add src/remem/agents/opencode/ tests/test_opencode_event.py pyproject.toml
git commit -m "Read opencode plugin payloads as events"
```

---

### Task 2: The vendored hook list

**Files:**
- Create: `src/remem/agents/opencode/hooks.py`
- Test: `tests/test_opencode_hooks_contract.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `HOOK_NAMES: frozenset[str]`, `PLUGIN_TYPES_VERSION: str`, and
  `installed_hook_names(root: Path) -> frozenset[str]`. Task 4 asserts the
  plugin's subscriptions are a subset of `HOOK_NAMES`.

**Context you need:** The spec's "The contract test" section. The split exists
because the naive single test `pytest.skip`s wherever opencode is not installed,
and a guard that evaporates on CI is not a guard. This task builds the half that
**always runs** plus the freshness check on our copy; Task 4 builds the half that
checks the plugin against it.

Add an `opencode` marker to `pyproject.toml` in this task, beside the existing
`db` and `slow` markers.

- [ ] **Step 1: Write the failing test**

Create `tests/test_opencode_hooks_contract.py`:

```python
"""Two questions, deliberately not one test.

  1. Does the plugin subscribe to something we believe is not real?
     (Task 4. Always runs, everywhere, no node_modules.)
  2. Is what we believe still true?
     (Here. Reads the installed types, and may skip.)

Collapsing them produces a guard that skips on CI, which is the failure the
`db` markers already taught this project to distrust.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remem.agents.opencode.hooks import (
    HOOK_NAMES,
    PLUGIN_TYPES_VERSION,
    installed_hook_names,
)

#: Where a normal `opencode` install puts the plugin types.
TYPES_ROOTS = (
    Path.home() / ".config" / "opencode" / "node_modules",
    Path.home() / ".opencode" / "node_modules",
)


def test_the_vendored_list_holds_the_hooks_we_subscribe_to():
    """A floor, not the whole list: HOOK_NAMES is every hook opencode has,
    and these three are the ones this adapter uses."""
    assert {
        "tool.execute.after",
        "chat.message",
        "experimental.chat.system.transform",
    } <= HOOK_NAMES


def test_the_vendored_list_records_where_it_came_from():
    assert PLUGIN_TYPES_VERSION == "1.3.5"


def test_parsing_a_hooks_interface(tmp_path):
    """The parser, exercised without needing opencode installed."""
    d = tmp_path / "@opencode-ai" / "plugin" / "dist"
    d.mkdir(parents=True)
    (d / "index.d.ts").write_text(
        'export type Plugin = () => Promise<Hooks>;\n'
        "export interface Hooks {\n"
        "    event?: (input: { event: Event }) => Promise<void>;\n"
        "    config?: (input: Config) => Promise<void>;\n"
        '    "chat.message"?: (input: {\n'
        "        sessionID: string;\n"
        "    }) => Promise<void>;\n"
        '    "tool.execute.after"?: (input: {}) => Promise<void>;\n'
        "}\n"
        "export interface Other {\n"
        '    "not.a.hook"?: () => void;\n'
        "}\n"
    )

    names = installed_hook_names(tmp_path)

    assert names == {"event", "config", "chat.message", "tool.execute.after"}
    assert "not.a.hook" not in names


@pytest.mark.opencode
def test_the_vendored_list_still_matches_the_installed_types():
    """Guards our copy of an external fact, not the plugin.

    This one may skip - what it protects is the freshness of HOOK_NAMES, and
    only a machine with opencode installed can answer it.
    """
    for root in TYPES_ROOTS:
        if (root / "@opencode-ai" / "plugin" / "dist" / "index.d.ts").exists():
            break
    else:
        pytest.skip(
            "opencode's plugin types are not installed; nothing to compare "
            "the vendored hook list against"
        )

    installed = installed_hook_names(root)

    assert installed == HOOK_NAMES, (
        "opencode's Hooks interface has changed. Update HOOK_NAMES and "
        "PLUGIN_TYPES_VERSION in src/remem/agents/opencode/hooks.py, then "
        "check whether plugin.js should subscribe to anything new."
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_opencode_hooks_contract.py -v`
Expected: FAIL, collection error - `ModuleNotFoundError: No module named 'remem.agents.opencode.hooks'`

- [ ] **Step 3: Register the pytest marker**

In `pyproject.toml`, under `[tool.pytest.ini_options]`, extend `markers`:

```toml
markers = [
    "db: requires a running Postgres (docker compose up -d)",
    "slow: shells out to a real build; skip with -m 'not slow'",
    "opencode: requires opencode's plugin types installed; checks our vendored copy is fresh",
]
```

- [ ] **Step 4: Write the vendored list**

Create `src/remem/agents/opencode/hooks.py`:

```python
"""Every hook name on opencode's `Hooks` interface, vendored.

This file is a copy of an external fact, checked in on purpose. The
alternative - reading ~/.opencode/node_modules at test time - makes the
contract test skip wherever opencode is not installed, which is everywhere
that matters, CI included. A guard that skips is not a guard.

So the checked-in list is what the plugin is tested against, and a separate
`opencode`-marked test asserts this list still matches the installed types.
The two answer different questions; see tests/test_opencode_hooks_contract.py.

Transcribed from @opencode-ai/plugin 1.3.5,
dist/index.d.ts, `export interface Hooks`.
"""

from __future__ import annotations

import re
from pathlib import Path

PLUGIN_TYPES_VERSION = "1.3.5"

HOOK_NAMES = frozenset(
    {
        "event",
        "config",
        "tool",
        "auth",
        "chat.message",
        "chat.params",
        "chat.headers",
        "permission.ask",
        "command.execute.before",
        "tool.execute.before",
        "shell.env",
        "tool.execute.after",
        "experimental.chat.messages.transform",
        "experimental.chat.system.transform",
        "experimental.session.compacting",
    }
)

#: A property line inside an interface body: an optional member, quoted or
#: bare. Deliberately crude - this parses one known file to refresh one
#: checked-in list, and a real TypeScript parse would be a dependency and a
#: maintenance burden out of all proportion to that.
_MEMBER = re.compile(r'^\s{4}"?([A-Za-z][\w.]*)"?\??\s*:', re.MULTILINE)


def installed_hook_names(root: Path) -> frozenset[str]:
    """Read the hook names off an installed @opencode-ai/plugin.

    `root` is a node_modules directory. Returns an empty set if the types
    are not there; the caller decides whether that is a skip or a failure,
    because the answer differs between the two tests that call this.
    """
    types = root / "@opencode-ai" / "plugin" / "dist" / "index.d.ts"
    if not types.exists():
        return frozenset()

    text = types.read_text()
    start = text.find("export interface Hooks {")
    if start == -1:
        return frozenset()
    # The interface body ends at the first line that closes it at column 0.
    end = text.find("\n}", start)
    body = text[start:end if end != -1 else len(text)]
    return frozenset(_MEMBER.findall(body))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_opencode_hooks_contract.py -v`
Expected: PASS, 4 passed. On this machine none should skip - opencode's types
are installed at `~/.opencode/node_modules`. A skip here means the freshness
test found nothing to read; confirm that is true before accepting it.

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest`
Expected: PASS, skip count 0.

- [ ] **Step 7: Commit**

```bash
git add src/remem/agents/opencode/hooks.py tests/test_opencode_hooks_contract.py pyproject.toml
git commit -m "Vendor opencode's hook names so the contract test never skips"
```

---

### Task 3: A harness-neutral context block

**Files:**
- Create: `src/remem/services/context.py`
- Modify: `src/remem/agents/claude_code/hook.py` (`session_start`, and remove `handoff_pointer`)
- Modify: `src/remem/cli.py` (new `remem hook context` command, near `src/remem/cli.py:769-800`)
- Modify: `tests/test_handoff_pointer.py` (import moves)
- Test: `tests/test_context_service.py`

**Interfaces:**
- Consumes: `remem.services.kb`, `remem.agents.base.Identity`, `remem.config.load`,
  `remem.session.open_session`, `remem.store.Store`.
- Produces: `remem.services.context.block(store, owner_id, project, max_chars) -> str`
  and `remem.services.context.handoff_pointer(store, owner_id, project) -> str`.
  Task 4's `plugin.js` calls the CLI this task adds.

**Why this task exists:** The spec assumes the plugin can ask for a knowledge
base block. It cannot today. `remem hook session-start` reads a *Claude Code*
payload and is hard-wired to `ClaudeCodeAdapter`, and the block-building logic
lives inside `hook.py`, which is a frontend. Recording was already harness-neutral
(`remem record event --agent ...`); injection never was. This task fixes that by
moving the decision into a service and adding one neutral command, so
`session_start` becomes a thin wrapper over the same code the opencode plugin
calls.

**Context you need:** Read `src/remem/agents/claude_code/hook.py:29-100` -
`session_start` and `handoff_pointer`. You are moving the body of the `try`
block's inner half into a service, unchanged in behaviour. Keep every comment;
they explain the two decisions that look arbitrary (why the pointer is appended
outside `max_chars`, and why the block is built rather than returned early when
there is no knowledge base).

- [ ] **Step 1: Write the failing test**

Create `tests/test_context_service.py`:

```python
"""The context block, once, for every harness.

Injection used to live inside the Claude Code hook, which meant a second
harness could record events but could not be told anything.
"""

from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import CollectionQuery
from remem.services import context, kb
from remem.services.write import remember

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def test_block_renders_a_knowledge_base(store, owner):
    kb.create(store, owner.id, slug="demo", title="Demo",
              query=CollectionQuery(tags=["style"]))
    remember(store, owner.id, title="A rule", body="Body text", tags=["style"])

    block = context.block(store, owner.id, "demo", max_chars=10_000)

    assert "A rule" in block


def test_block_is_empty_for_a_project_with_no_knowledge_base(store, owner):
    """Silence, not an exception. Every caller of this is fail-soft."""
    assert context.block(store, owner.id, "nothing-here", max_chars=10_000) == ""


def test_the_reason_for_an_empty_block_is_reported(store, owner):
    """Silence is ambiguous, which is why REMEM_HOOK_DEBUG exists. The
    service knows why it returned nothing; only the frontend knows where to
    say so, hence the callable."""
    said: list[str] = []

    context.block(store, owner.id, "nothing-here", max_chars=10_000, note=said.append)

    assert any("nothing-here" in line for line in said)
```

The fixtures above match `tests/test_kb_resolve.py:12-20` exactly - `conn` from
`tests/conftest.py` rolls back, and `store` / `owner` are built per test file.
There is no shared `store` fixture; do not invent one.

Add a fourth test for the handoff pointer being appended outside `max_chars`,
modelled on `tests/test_handoff_pointer.py` - read that file for the handoff
write helper it uses rather than guessing at a signature.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_context_service.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'remem.services.context'`

- [ ] **Step 3: Create the service**

Create `src/remem/services/context.py`, moving the body of `session_start`'s
inner half and all of `handoff_pointer` out of
`src/remem/agents/claude_code/hook.py`. The service takes a store and a project
name and returns a string; it opens no session and reads no payload, because
those are the two things that differ between harnesses.

The signature, which Task 4's plugin and the rewritten `session_start` both
depend on:

```python
def block(
    store: Store,
    owner_id: UUID,
    project: str,
    max_chars: int,
    note: Callable[[str], None] | None = None,
) -> str:
    """The knowledge base context block for one project, or "".

    Returns "" rather than raising for a project with no knowledge base:
    every caller is a fail-soft hook, and a missing knowledge base is an
    ordinary state, not an error.

    `note` is how the reason escapes. The debug messages this replaces went
    straight to REMEM_HOOK_DEBUG, which needs `env` - and a service that
    took `env` to decide where to print would be a service formatting
    output. So the service says what happened and the frontend decides
    where it goes: the Claude Code hook passes its `_debug`, the CLI passes
    its own, and a test passes a list's `append`.
    """
```

Keep the `kb.CollectionNotFound` branch and every existing debug message,
routed through `note` instead of `_debug`. Keep the two comments that explain
the non-obvious decisions: why the handoff pointer is appended after render and
outside `max_chars`, and why the block is built rather than returned early when
there is no knowledge base at all.

- [ ] **Step 4: Rewrite `session_start` to use it**

`session_start` keeps its signature, its `try`/`except Exception` wrapper, its
fail-soft contract and its debug calls. Its body becomes: parse stdin, build the
`Identity` via `ClaudeCodeAdapter`, `load()` the config, `open_session`, call
`context.block(...)`, return. Update `tests/test_handoff_pointer.py` to import
`handoff_pointer` from `remem.services.context` rather than from the hook.

- [ ] **Step 5: Add the neutral CLI command**

In `src/remem/cli.py`, beside the other `hook_app` commands:

```python
@hook_app.command("context")
def hook_context(
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
):
    """Print the knowledge base block for the session on stdin.

    The harness-neutral half of what SessionStart does for Claude Code. A
    harness with no session-start hook - opencode, Cursor - calls this
    instead, passing whatever payload it has; the adapter's identity()
    turns that into a project.

    Fail-soft like every hook: exits 0 unconditionally, and prints the
    block and nothing else to stdout. Reasons go to stderr under
    REMEM_HOOK_DEBUG.
    """
```

Its body mirrors `record_event` at `src/remem/cli.py:1078`: read stdin, parse
JSON (debug and exit 0 on failure), `registry.get(agent)` (debug and exit 0 on
`UnknownAgent`), `adapter.identity(env, payload)`, and if there is a project,
open a session and echo `context.block(...)`. Wrap the whole thing so no
exception escapes. `identity()` is a **required** Protocol method, so unlike
`event()` it needs no `getattr` probe - but a third-party adapter whose
`identity()` raises must still only cost the block, so catch broadly and debug.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_context_service.py tests/test_handoff_pointer.py tests/test_hook.py -v`
Expected: PASS

- [ ] **Step 7: Check the command by hand**

```bash
echo '{"cwd":"'"$PWD"'","session_id":"x"}' | remem hook context --agent claude-code
echo '{"cwd":"'"$PWD"'","sessionID":"x"}' | remem hook context --agent opencode
```

Expected: both print the same block (or both print nothing, if this repository
has no knowledge base). They read different session-id keys and the same cwd,
which is exactly the difference the adapters exist to absorb. Both must exit 0:
check with `echo $?`.

- [ ] **Step 8: Run the whole suite**

Run: `uv run pytest`
Expected: PASS, skip count 0.

- [ ] **Step 9: Commit**

```bash
git add src/remem/services/context.py src/remem/agents/claude_code/hook.py src/remem/cli.py tests/test_context_service.py tests/test_handoff_pointer.py
git commit -m "Make knowledge base injection harness-neutral"
```

---

### Task 4: The plugin, and the contract test that guards it

**Files:**
- Create: `src/remem/agents/opencode/plugin.js`
- Modify: `tests/test_opencode_hooks_contract.py` (add the always-runs half)

**Interfaces:**
- Consumes: `HOOK_NAMES` from Task 2; the payload contract from Task 1
  (`{"hook", "sessionID", "cwd", ...}`); the CLI commands
  `remem record event --agent opencode` and `remem hook context --agent opencode`
  from Task 3.
- Produces: `plugin.js`, loaded as package data by Task 5's installer.

**Context you need:** `plugin.js` imports nothing. `$` is a Bun shell that
arrives on `PluginInput`, which is what makes the file dependency-free.
`@opencode-ai/plugin` must not appear in an `import`.

Two things about Bun's shell that the code below works around, both worth
understanding before you edit it:

- A `ShellPromise` cannot be killed. `Promise.race` with a timer lets *our*
  hook return on schedule, but the child may outlive it. That is the right
  trade - what must not happen is the user's turn hanging - and it is why the
  timeout is a race rather than a kill.
- stdin is redirected from a `Response`, which is Bun's documented way to feed
  a string to a command.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_opencode_hooks_contract.py`:

```python
def _plugin_source() -> str:
    from importlib import resources

    return (
        resources.files("remem.agents.opencode").joinpath("plugin.js").read_text()
    )


#: Keys of the object plugin.js returns from `server`. Matches a quoted or
#: bare property at one indent level inside the returned object literal.
_SUBSCRIBED = re.compile(r'^\s{4}"?([a-z][\w.]*)"?\s*[:(]', re.MULTILINE)


def test_the_plugin_subscribes_only_to_hooks_opencode_emits():
    """The regression guard. Always runs - no node_modules, no skip.

    claude-mem shipped an opencode integration bound to event names opencode
    does not emit, and it recorded nothing for months without ever failing.
    """
    subscribed = frozenset(_SUBSCRIBED.findall(_plugin_source()))

    assert subscribed, "found no hook subscriptions in plugin.js - the parser is broken"
    assert subscribed <= HOOK_NAMES, (
        f"plugin.js subscribes to hooks opencode does not emit: "
        f"{sorted(subscribed - HOOK_NAMES)}"
    )


def test_the_plugin_subscribes_to_exactly_the_three_hooks_the_design_names():
    subscribed = frozenset(_SUBSCRIBED.findall(_plugin_source()))

    assert subscribed == {
        "tool.execute.after",
        "chat.message",
        "experimental.chat.system.transform",
    }


def test_the_plugin_imports_nothing():
    """The whole point of the approach: no npm dependency at runtime, so
    there is no version to keep in step and nothing to install."""
    source = _plugin_source()

    assert "import " not in source
    assert "require(" not in source


def test_the_plugin_says_it_is_generated():
    assert _plugin_source().startswith("// generated by remem - do not edit")
```

Add `import re` to the test module's imports.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_opencode_hooks_contract.py -v`
Expected: FAIL - `FileNotFoundError` / `plugin.js` is not there.

- [ ] **Step 3: Write the plugin**

Create `src/remem/agents/opencode/plugin.js`:

```javascript
// generated by remem - do not edit. `remem install --agent opencode`
// overwrites this file unconditionally.
//
// A marshaller and nothing else: it shapes opencode's hook arguments into
// JSON and hands them to the `remem` CLI, which decides everything. It
// imports nothing - `$` arrives on PluginInput - so there is no npm
// dependency to install, to pin, or to keep in step with this file.

const RECORD_TIMEOUT_MS = 5000;
const CONTEXT_TIMEOUT_MS = 10000;

// Sessions whose knowledge base block has already been injected.
//
// This is the one policy decision that lives in the JavaScript, and it is
// here under protest: experimental.chat.system.transform fires on EVERY
// message, opencode has no session-start hook, and per-process state has
// nowhere else to live. Claude Code gets once-per-session semantics from
// its harness; opencode has to be given them.
//
// It dies with the process, which is the correct lifetime: a restarted
// opencode is a new session's worth of context anyway.
const injected = new Set();

// Run a command, feed it JSON on stdin, and give up after `timeoutMs`.
//
// The timeout is a race, not a kill: Bun's ShellPromise cannot be
// cancelled, so a hung `remem` may outlive this call. That is deliberate.
// What must never happen is the user's turn hanging on a knowledge tool,
// and returning on schedule is what prevents it.
//
// Every failure is swallowed. This is the same fail-soft contract the
// Python hooks hold, except that nothing here inherits a harness timeout,
// so the plugin has to impose its own.
async function callRemem($, args, stdinText, timeoutMs) {
  let timer;
  try {
    const running = $`remem ${args} < ${new Response(stdinText)}`
      .quiet()
      .nothrow();
    const timeout = new Promise((resolve) => {
      timer = setTimeout(() => resolve(null), timeoutMs);
    });
    const result = await Promise.race([running, timeout]);
    if (result === null) return null;
    return result.exitCode === 0 ? result.stdout.toString() : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

async function record($, cwd, payload) {
  await callRemem(
    $,
    ["record", "event", "--agent", "opencode"],
    JSON.stringify({ ...payload, cwd }),
    RECORD_TIMEOUT_MS,
  );
}

export const server = async ({ $, directory, worktree }) => {
  // `worktree` is the repository root when there is one; `directory` is
  // where the session was started. Either is fine - remem resolves the
  // project through the git common directory on the Python side, so a
  // subdirectory and a worktree file under the same repository - and
  // preferring worktree just means the common case does less work.
  const cwd = worktree || directory;

  return {
    "tool.execute.after": async (input, output) => {
      await record($, cwd, {
        hook: "tool.execute.after",
        sessionID: input.sessionID,
        tool: input.tool,
        callID: input.callID,
        args: input.args,
        result: {
          title: output.title,
          output: output.output,
          metadata: output.metadata,
        },
      });
    },

    "chat.message": async (input, output) => {
      await record($, cwd, {
        hook: "chat.message",
        sessionID: input.sessionID,
        agent: input.agent,
        model: input.model,
        messageID: input.messageID,
        message: output.message,
        parts: output.parts,
      });
    },

    "experimental.chat.system.transform": async (input, output) => {
      // Once per session. A memory written mid-session is therefore not
      // visible until the next one - the same bargain the Claude Code
      // SessionStart hook makes. Re-reading every turn is one line's
      // change from here, and is deliberately not made until the per-turn
      // token and database cost has been measured.
      const id = input.sessionID;
      if (!id || injected.has(id)) return;
      injected.add(id);

      const block = await callRemem(
        $,
        ["hook", "context", "--agent", "opencode"],
        JSON.stringify({ sessionID: id, cwd }),
        CONTEXT_TIMEOUT_MS,
      );
      if (block && block.trim()) output.system.push(block.trim());
    },
  };
};
```

- [ ] **Step 4: Confirm the plugin ships in the wheel**

`pyproject.toml` already notes that hatchling ships every VCS-tracked file under
`src/remem`, which is how `migrations/*.sql` and the skills directories travel.
`plugin.js` is tracked, so it needs no `pyproject.toml` change - but prove it
rather than assuming:

Run: `uv build && uv run python -c "import zipfile,glob; print([n for n in zipfile.ZipFile(sorted(glob.glob('dist/*.whl'))[-1]).namelist() if 'plugin.js' in n])"`
Expected: `['remem/agents/opencode/plugin.js']`

If the list is empty, add the file to hatchling's `force-include` and re-run.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_opencode_hooks_contract.py -v`
Expected: PASS, 8 passed

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest`
Expected: PASS, skip count 0.

- [ ] **Step 7: Commit**

```bash
git add src/remem/agents/opencode/plugin.js tests/test_opencode_hooks_contract.py
git commit -m "Add the opencode plugin: a marshaller that imports nothing"
```

---

### Task 5: Installing the plugin

**Files:**
- Create: `src/remem/agents/opencode/install.py`
- Modify: `src/remem/agents/opencode/adapter.py` (add `install()`)
- Modify: `src/remem/cli.py:563-590` (`--agent` on `install`)
- Test: `tests/test_opencode_install.py`

**Interfaces:**
- Consumes: `OpenCodeAdapter` from Task 1; `plugin.js` from Task 4;
  `remem.agents.base.InstallReport`, `UnsupportedScope`.
- Produces: `OpenCodeAdapter.install(scope, home, env) -> InstallReport`, and
  `plugin_dir(scope: str, home: Path, cwd: Path) -> Path` in `install.py`.

**Context you need:** Read `src/remem/cli.py:563-590` - `install` currently
constructs `ClaudeCodeAdapter` directly or resolves a fixed name; make it resolve
`--agent` through `registry.get`, defaulting to `claude-code` so existing
behaviour and every existing test are unchanged.

Unlike Claude Code, this adapter supports **both** scopes: `user` writes to
`~/.config/opencode/plugin/`, `project` writes to `.opencode/plugin/` under the
current directory. Anything else raises `UnsupportedScope` - never a fallback.
That is a deliberate difference from the Claude Code adapter, which raises for
`project`, and it exists because opencode's per-project plugin directory is a
documented convention rather than something remem would be inventing.

- [ ] **Step 1: Write the failing test**

Create `tests/test_opencode_install.py`:

```python
"""Writing the plugin, and overwriting it.

remem owns plugin.js: no version marker, no merge, no prompt. The file is
machine-generated and machine-replaced, and the install says where it went.
"""

from __future__ import annotations

import pytest

from remem.agents.base import UnsupportedScope
from remem.agents.opencode.adapter import OpenCodeAdapter


def test_user_scope_writes_the_plugin_under_the_config_directory(tmp_path):
    report = OpenCodeAdapter().install(scope="user", home=tmp_path, env={})

    plugin = tmp_path / ".config" / "opencode" / "plugin" / "remem.js"
    assert plugin.exists()
    assert plugin.read_text().startswith("// generated by remem - do not edit")
    assert any(str(plugin) in action for action in report.actions)


def test_project_scope_writes_beside_the_repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    OpenCodeAdapter().install(scope="project", home=tmp_path, env={})

    assert (tmp_path / ".opencode" / "plugin" / "remem.js").exists()


def test_an_unknown_scope_refuses_rather_than_guessing(tmp_path):
    """UnsupportedScope over a fallback: an install that reports success
    while having written somewhere else is worse than one that refuses."""
    with pytest.raises(UnsupportedScope):
        OpenCodeAdapter().install(scope="global", home=tmp_path, env={})


def test_a_second_install_overwrites_a_modified_plugin(tmp_path):
    """No version marker, unconditional overwrite. remem owns the file."""
    adapter = OpenCodeAdapter()
    adapter.install(scope="user", home=tmp_path, env={})
    plugin = tmp_path / ".config" / "opencode" / "plugin" / "remem.js"
    plugin.write_text("// someone edited this\n")

    adapter.install(scope="user", home=tmp_path, env={})

    assert plugin.read_text().startswith("// generated by remem - do not edit")


def test_the_install_never_touches_opencode_config(tmp_path):
    """The plugin directory IS the registration, so there is no user config
    file to merge or corrupt. That is a property worth pinning."""
    config = tmp_path / ".config" / "opencode" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"model": "anthropic/claude-opus-5"}')

    OpenCodeAdapter().install(scope="user", home=tmp_path, env={})

    assert config.read_text() == '{"model": "anthropic/claude-opus-5"}'


def test_the_install_reports_the_opt_in_gate(tmp_path):
    """Recording is off until the user enables it per project, and an
    install that does not say so leaves them waiting for events that will
    never come."""
    report = OpenCodeAdapter().install(scope="user", home=tmp_path, env={})

    assert any("record enable" in note for note in report.notes)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_opencode_install.py -v`
Expected: FAIL - `AttributeError: 'OpenCodeAdapter' object has no attribute 'install'`

- [ ] **Step 3: Write the installer**

Create `src/remem/agents/opencode/install.py` with `plugin_dir(scope, home, cwd)`
returning `home / ".config" / "opencode" / "plugin"` for `"user"` and
`cwd / ".opencode" / "plugin"` for `"project"`, raising `UnsupportedScope` for
anything else. Comment why the config directory is not read from an environment
variable the way `CLAUDE_CONFIG_DIR` is: opencode has no documented equivalent,
and inventing one would create a path remem honours and opencode ignores.

Add `install()` to `OpenCodeAdapter`: resolve the directory, `mkdir(parents=True,
exist_ok=True)`, read `plugin.js` through `importlib.resources.files`, write it
to `remem.js`, and append to the report - the path written, plus the same
`RECORD_NOTE` about the per-project opt-in that the Claude Code adapter prints
(import it from `remem.agents.claude_code.adapter`, or lift it to
`remem/agents/base.py` if that import reads wrong to you; prefer lifting, since
it is a fact about remem rather than about Claude Code).

- [ ] **Step 4: Wire up `--agent`**

In `src/remem/cli.py`'s `install` command, add
`agent: Annotated[str, typer.Option("--agent")] = "claude-code"` and resolve it
through `registry.get(agent)()`, catching `registry.UnknownAgent` and exiting
with a message that names the available agents. Leave the default and the
existing output untouched so no existing test changes.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_opencode_install.py tests/test_claude_code_install.py tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest`
Expected: PASS, skip count 0.

- [ ] **Step 7: Commit**

```bash
git add src/remem/agents/opencode/install.py src/remem/agents/opencode/adapter.py src/remem/cli.py tests/test_opencode_install.py
git commit -m "Install the opencode plugin, in either scope"
```

---

### Task 6: One install verification, shared by both adapters

**Files:**
- Create: `src/remem/agents/verify.py`
- Modify: `src/remem/agents/claude_code/adapter.py:287-371` (delegate `verify()`)
- Modify: `src/remem/agents/opencode/adapter.py` (add `verify()`, call it from `install()`)
- Test: `tests/test_opencode_verify.py`

**Interfaces:**
- Consumes: `remem.services.record`, `remem.session.open_session`,
  `remem.config.load`, `remem.agents.base.HarnessEvent` / `InstallReport`.
- Produces: `remem.agents.verify.round_trip(agent_name: str, env: Mapping[str, str] | None) -> InstallReport`.
- `VERIFY_PROJECT` moves to `remem/agents/verify.py`; re-export it from
  `remem.agents.claude_code.adapter` so existing imports keep working, and say
  in a comment that the re-export is for compatibility.

**Context you need:** Read `ClaudeCodeAdapter.verify` at
`src/remem/agents/claude_code/adapter.py:287-371`. Every line of it is
harness-independent except `self.name`. This task lifts it out whole.

**Why it belongs in this plan:** the docstring of that method names claude-mem's
opencode integration as the failure it exists to prevent. The opencode adapter is
literally that case. Shipping it without the round-trip would reproduce the exact
bug the code was written against.

Preserve, unchanged: the `finally` block that deletes through
`store.delete_session_events` scoped to all four keys - **never**
`services.events.prune`, whose contract is a time window over every event the
owner has - and the `except Exception` that turns any failure into a warning
rather than a dead install.

- [ ] **Step 1: Write the failing test**

Create `tests/test_opencode_verify.py`:

```python
"""The live round-trip, for the harness it was written about.

claude-mem's opencode integration reported success for months while
recording nothing. This is the check that would have caught it.
"""

from __future__ import annotations

import psycopg
import pytest

from remem.agents.opencode.adapter import OpenCodeAdapter
from remem.agents.verify import VERIFY_PROJECT
from remem.backends.postgres.migrate import migrate

pytestmark = pytest.mark.db


@pytest.fixture
def env(live_dsn, tmp_path):
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    return {
        "REMEM_DSN": live_dsn,
        "REMEM_USER_ID": "brandon",
        "REMEM_CONFIG": str(tmp_path / "none.toml"),
    }


def test_verification_round_trips_a_real_event(env, tmp_path):
    report = OpenCodeAdapter().verify(env=env, home=tmp_path)

    assert any("round-trip" in a for a in report.actions)
    assert report.warnings == []


def test_verification_records_under_the_opencode_harness(env, tmp_path):
    """Not 'claude-code'. The harness column is what `remem record status`
    groups by, so a shared round-trip that hardcoded a name would report
    the wrong harness as working."""
    from remem.config import load
    from remem.session import open_session

    report = OpenCodeAdapter().verify(env=env, home=tmp_path)

    assert report.warnings == []
    with open_session(load(env=env)) as s:
        assert s.store.enabled_record_projects(s.owner.id) == []


def test_verification_leaves_another_principal_untouched(env, live_dsn, tmp_path):
    """The cross-owner assertion, and the reason this file exists at all.

    A scratch database holding nothing else is structurally incapable of
    catching an over-broad delete: absence of other rows is exactly what the
    fixture guarantees. So another principal's event has to be present, and
    has to be asserted surviving BY ID. Asserting the target is gone pins
    nothing.

    This is the shape of tests/test_events_prune.py's cross-owner test,
    adopted after install verification cleaned up with prune(force=True) and
    would have destroyed every user's entire event history.
    """
    from datetime import datetime, timezone

    from remem.agents.base import HarnessEvent
    from remem.domain import EventKind

    with psycopg.connect(live_dsn) as c:
        other_store = PostgresStore(c)
        other = other_store.ensure_principal("someone-else")
        theirs = other_store.put_event(
            Event(
                owner_id=other.id,
                project=VERIFY_PROJECT,
                harness="opencode",
                session_id="theirs",
                kind=EventKind.TOOL_CALL,
                payload={"mine": False},
                occurred_at=datetime.now(timezone.utc),
            )
        )
        c.commit()

    report = OpenCodeAdapter().verify(env=env, home=tmp_path)
    assert report.warnings == []

    with psycopg.connect(live_dsn) as c:
        survived = PostgresStore(c).events_for_session(
            other.id, VERIFY_PROJECT, "opencode", "theirs"
        )
    assert [e.id for e in survived] == [theirs.id]
```

Add `from remem.backends.postgres.store import PostgresStore` and the `Event`
import to the module's imports. Construct the other principal's event the way
`tests/test_events_prune.py:_seed_prunable` and its `an_event` helper do - read
that file and match its constructor call exactly rather than trusting the field
list above, which is written from the schema and not from the code.

Note the deliberate cruelty of the fixture: the other principal's event is in
**`VERIFY_PROJECT`, the same session-adjacent project verification uses**, and
under the same harness. A delete scoped to three of the four keys would pass a
test that put the other event somewhere else entirely; only same-project,
same-harness, different-owner-and-session pins all four.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_opencode_verify.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'remem.agents.verify'`

- [ ] **Step 3: Lift the round-trip into a shared module**

Create `src/remem/agents/verify.py` holding `VERIFY_PROJECT` and
`round_trip(agent_name, env=None)`, which is `ClaudeCodeAdapter.verify`'s body
with `self.name` replaced by `agent_name`. Move every comment with it.

- [ ] **Step 4: Delegate from both adapters**

`ClaudeCodeAdapter.verify` becomes a one-line delegation to
`verify.round_trip(self.name, env)`, keeping its `home` parameter and its
docstring's note that `home` is accepted for symmetry and unused. Add the same
method to `OpenCodeAdapter`, and call it from the end of `OpenCodeAdapter.install()`
exactly as the Claude Code adapter does: last, never raising, folding actions and
warnings into the install's own report.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_opencode_verify.py tests/test_claude_code_events_install.py -v`
Expected: PASS. Both adapters' verification tests run against the same lifted code.

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest`
Expected: PASS, skip count 0.

- [ ] **Step 7: Commit**

```bash
git add src/remem/agents/verify.py src/remem/agents/claude_code/adapter.py src/remem/agents/opencode/adapter.py tests/test_opencode_verify.py
git commit -m "Share the install round-trip between both adapters"
```

---

### Task 7: Prove it against real opencode, then document it

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-08-29-opencode-adapter-design.md` (status line)

**Interfaces:**
- Consumes: everything above.
- Produces: no code.

This task is the one that cannot be faked by a passing suite. `opencode` is on
PATH and the plugin types are installed at 1.3.5, so the adapter can be exercised
for real - and the whole design exists because an integration that reported
success while recording nothing shipped for months.

- [ ] **Step 1: Install into a scratch project**

```bash
mkdir -p /tmp/remem-opencode-check && cd /tmp/remem-opencode-check && git init -q
remem install --agent opencode --scope project
remem kb new remem-opencode-check
remem record enable --project remem-opencode-check
```

Expected: the install names the file it wrote, and the note about the per-project
opt-in appears.

- [ ] **Step 2: Write something for the knowledge base to inject**

```bash
remem note "Injection check" --body "If you can read this, the block was injected." --project remem-opencode-check
```

Use whatever the current `remem` write command is - check `remem --help` rather
than trusting this line.

- [ ] **Step 3: Run a real opencode session**

```bash
cd /tmp/remem-opencode-check && opencode
```

Ask it to run one shell command, then ask it what it knows about this project.
Expected: it can quote the injection check note, which proves
`experimental.chat.system.transform` fired and the block landed.

- [ ] **Step 4: Assert the events actually arrived**

```bash
remem record status
remem events show --project remem-opencode-check
```

Expected: `record status` lists the `opencode` harness with a non-zero count, and
`events show` lists at least one `tool_call` and one `message`. **A zero here is
the failure this entire design was written to prevent** - do not proceed past it,
and check `REMEM_HOOK_DEBUG=1 remem record event --agent opencode < payload.json`
by hand to find out why.

- [ ] **Step 5: Assert the block is injected once, not per turn**

Send several messages in one session, then confirm the system prompt did not grow
a block per turn - the plugin's `Set` should mean exactly one. If opencode offers
no way to inspect the system prompt, assert it indirectly: the block is fetched by
a `remem hook context` call, so a second call within one session would show up as
a second row's worth of work. Record what you actually checked.

- [ ] **Step 6: Clean up**

```bash
remem record disable --project remem-opencode-check
rm -rf /tmp/remem-opencode-check
```

- [ ] **Step 7: Document it**

In `README.md`, add opencode beside Claude Code in the install section:
`remem install --agent opencode` (both scopes), the plugin path, the per-project
opt-in, and the once-per-session injection with its consequence stated plainly -
a memory written mid-session is visible next session.

In `CLAUDE.md`, extend the architecture notes with: the adapter list now has two
entries; `plugin.js` imports nothing and is overwritten unconditionally; the
contract test is two tests and why; and that `remem hook context --agent` is the
harness-neutral half of SessionStart.

Set the spec's `Status:` line to `implemented`.

- [ ] **Step 8: Run the whole suite one last time**

Run: `docker compose ps && uv run pytest`
Expected: Postgres healthy on 5433, PASS, skip count 0.

- [ ] **Step 9: Commit**

```bash
git add README.md CLAUDE.md docs/superpowers/specs/2026-08-29-opencode-adapter-design.md
git commit -m "Document the opencode adapter"
```

---

## Notes for the reviewer

Three things a task-scoped reviewer will not see, and which the final
whole-branch review should check:

1. **Task 3 changes Claude Code's behaviour.** `session_start` is rewritten to
   call a service that did not exist. Its fail-soft contract - exit 0, print
   nothing on error, never raise - must survive the move. `tests/test_hook.py`
   is the guard; confirm it still covers the unreachable-database and
   missing-knowledge-base paths after the refactor, rather than only that the
   happy path still works.
2. **Task 6 rewrites a delete path.** The lifted `round_trip` keeps the
   four-key `delete_session_events` and must never reach for
   `services.events.prune`. The cross-owner test in Task 6 Step 1 is
   non-negotiable; a scratch database proves nothing about scoping on its own.
3. **The payload contract spans Tasks 1 and 4.** `plugin.js` emits the keys
   `adapter.event()` reads, and nothing type-checks the seam. If either side is
   edited, `tests/test_opencode_event.py` is the only thing standing between a
   rename and a harness that records nothing, silently.
