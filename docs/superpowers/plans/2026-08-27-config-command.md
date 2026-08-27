# remem config Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One `remem config` command that reads and writes both remem's own settings and a curated set of Claude Code environment variables, reporting where every value actually comes from.

**Architecture:** A new `services/settings.py` owns all policy - routing by key name, validation, precedence reporting, and safe writes. The Claude Code env-var table is data supplied by the adapter (`env_settings()`), not by the service, so the service never learns any agent's schema. JSON read/backup/write moves out of the adapter into an agent-neutral `remem/jsonfile.py` shared by both. The CLI parses and formats only.

**Tech Stack:** Python 3.14, Typer, `tomllib` (read) + `tomli-w` (write), pytest. No database - this is the one service that touches no store.

**Spec:** `docs/superpowers/specs/2026-08-27-config-command-design.md`

## Global Constraints

- Python 3.14. `from __future__ import annotations` at the top of every module.
- Comments explain *why*, at length, especially where a decision looks arbitrary. A subtle invariant with no comment reads as an accident to the next reader.
- In prose, docs, comments and CLI output: spaced hyphens ` - `, never em dashes.
- Strict layering, downward calls only: `cli.py` -> `services/settings.py` -> adapter table / `jsonfile.py`. **Frontends parse and format; they never decide.** A policy branch in `cli.py` is a bug.
- Environment is always injected as a `Mapping[str, str]` parameter, never read from `os.environ` inside a service or a test. This is the pattern `config.load()` and `adapter.install()` already use.
- Fail-loud: raise, and let the CLI exit non-zero with a message on stderr. This is an explicit user command, not a hook. Hooks are fail-soft; this is not a hook.
- No test in this plan requires Postgres. None of them may be marked `db`.
- A green pytest run means nothing unless the skip count is zero. Check it.
- Run tests with `uv run pytest`.

---

### Task 1: Extract the safe-JSON-edit helpers into `remem/jsonfile.py`

Pure refactor. No behaviour change, no new feature. Doing it first means Task 5 has a shared helper to call instead of a second copy of the backup logic.

**Files:**
- Create: `src/remem/jsonfile.py`
- Modify: `src/remem/agents/claude_code/adapter.py` (remove `_backup`, `_backup_once`, `_read_json`, `_write_json`; import from the new module)
- Test: `tests/test_jsonfile.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `backup(path: Path) -> Path`
  - `backup_once(path: Path, backed_up: set[Path]) -> None`
  - `read_json(path: Path, backed_up: set[Path]) -> tuple[dict, list[str]]` - returns `(data, warnings)`; the warnings list is empty unless the file was unparseable, in which case it was backed up and replaced.
  - `write_json(path: Path, data: dict, backed_up: set[Path]) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_jsonfile.py`:

```python
from __future__ import annotations

import json

from remem import jsonfile


def test_read_json_returns_empty_for_a_missing_file(tmp_path):
    data, warnings = jsonfile.read_json(tmp_path / "nope.json", set())
    assert data == {}
    assert warnings == []


def test_read_json_backs_up_and_warns_on_invalid_json(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not valid json")
    backed_up: set = set()

    data, warnings = jsonfile.read_json(path, backed_up)

    assert data == {}
    assert len(warnings) == 1
    assert "backed up" in warnings[0]
    assert list(tmp_path.glob("settings.json.bak*"))


def test_write_json_preserves_unrelated_keys_via_the_caller(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"theme": "dark"}))
    backed_up: set = set()

    data, _ = jsonfile.read_json(path, backed_up)
    data["hooks"] = {}
    jsonfile.write_json(path, data, backed_up)

    assert json.loads(path.read_text()) == {"theme": "dark", "hooks": {}}


def test_backup_once_does_not_back_up_the_same_file_twice(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}")
    backed_up: set = set()

    jsonfile.backup_once(path, backed_up)
    jsonfile.backup_once(path, backed_up)

    assert len(list(tmp_path.glob("settings.json.bak*"))) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jsonfile.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'remem.jsonfile'`

- [ ] **Step 3: Write minimal implementation**

Create `src/remem/jsonfile.py` by moving the four functions out of `adapter.py`. The only change is the signature of `read_json`: it returns warnings instead of appending to an `InstallReport`, so that a caller with no report can use it.

```python
"""Safe edit-in-place of a JSON config file remem does not own.

Agent-neutral on purpose: both the Claude Code adapter (writing hooks at
install time) and services/settings.py (writing the env block later) edit the
same settings.json, and a second copy of the backup logic is how the two
drift apart.

read_json returns warnings rather than appending to an InstallReport: the
settings service has no report to append to, and coupling a file helper to an
install-time dataclass is what kept this logic trapped in the adapter.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path


def backup(path: Path) -> Path:
    ts = int(time.time())
    target = path.with_suffix(path.suffix + f".bak{ts}")
    counter = 0
    while target.exists():
        counter += 1
        target = path.with_suffix(path.suffix + f".bak{ts}-{counter}")
    shutil.copy2(path, target)
    return target


def backup_once(path: Path, backed_up: set[Path]) -> None:
    """Back up path if it exists on disk and hasn't already been backed up
    during this run (avoids a redundant second backup of a file read_json
    already snapshotted because it was corrupt)."""
    if path.exists() and path not in backed_up:
        backup(path)
        backed_up.add(path)


def read_json(path: Path, backed_up: set[Path]) -> tuple[dict, list[str]]:
    if not path.exists():
        return {}, []
    raw = path.read_text()
    try:
        return json.loads(raw), []
    except json.JSONDecodeError:
        backup_once(path, backed_up)
        return {}, [
            f"{path} was not valid JSON. It has been backed up and replaced; "
            "check the backup for anything you need."
        ]


def write_json(path: Path, data: dict, backed_up: set[Path]) -> None:
    backup_once(path, backed_up)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_jsonfile.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Rewire the adapter to the shared helpers**

In `src/remem/agents/claude_code/adapter.py`: delete the four private functions, add `from remem import jsonfile`, and replace call sites. `_read_json` had a `report` parameter that collected the warning; the adapter now does that itself:

```python
    def _install_mcp(
        self, paths: ClaudePaths, report: InstallReport, backed_up: set[Path]
    ) -> None:
        path = paths.global_json
        config, warnings = jsonfile.read_json(path, backed_up)
        report.warnings.extend(warnings)
        servers = config.setdefault("mcpServers", {})
        servers["remem"] = {"command": "remem", "args": ["serve"]}
        jsonfile.write_json(path, config, backed_up)
        report.actions.append(f"Registered the remem MCP server in {path}")
```

Apply the same two-line change (`read_json` returning a tuple, `report.warnings.extend`) in `_install_hook`. Remove the now-unused `import json`, `import shutil`, and `import time` **only if** nothing else in the file uses them - `shutil.copytree` in `_install_skill` still does, so `shutil` stays.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, same count as before plus the 4 new ones, 0 skipped. `tests/test_claude_code_install.py` exercises the moved code and must stay green without edits - that is the proof this was a pure refactor.

Note: `tests/test_claude_code_install.py` imports `_backup` directly (`from remem.agents.claude_code.adapter import _backup`). Update that import to `from remem.jsonfile import backup` and the call accordingly.

- [ ] **Step 7: Commit**

```bash
git add src/remem/jsonfile.py src/remem/agents/claude_code/adapter.py tests/test_jsonfile.py tests/test_claude_code_install.py
git commit -m "Extract safe JSON file editing into remem/jsonfile.py"
```

---

### Task 2: The env-var table on the Claude Code adapter

Data only. No routing, no validation, no writes - those are Task 3.

**Files:**
- Modify: `src/remem/agents/base.py` (add `Kind` and `EnvVar`; document the optional capability)
- Create: `src/remem/agents/claude_code/env_vars.py`
- Modify: `src/remem/agents/claude_code/adapter.py` (add `env_settings()` method)
- Test: `tests/test_env_vars.py`

**Interfaces:**
- Consumes: nothing.
- Produces, in `agents/base.py`:
  - `class Kind(StrEnum)` with members `INT`, `BOOL`, `PRESENCE`, `STR`
  - `@dataclass(frozen=True, slots=True) class EnvVar` with fields `name: str`, `kind: Kind`, `help: str`, `minimum: int | None = None`, `maximum: int | None = None`, `default: str | None = None`, `duration: bool = False`, `note: str | None = None`
- Produces, in `agents/claude_code/env_vars.py`:
  - `CLAUDE_CODE_ENV_VARS: dict[str, EnvVar]`
  - `ClaudeCodeAdapter.env_settings() -> Mapping[str, EnvVar]`

**Why `EnvVar` and `Kind` live in `agents/base.py`, not next to the table:**
`services/settings.py` needs both types to validate a value, and a service
importing `agents.claude_code.*` would invert the pluggability seam - the
service layer would depend on one specific adapter, which is what
`agents/registry.py` exists to prevent. `base.py` is the neutral vocabulary
both sides already share, alongside `Identity` and `InstallReport`. The
*table* stays adapter-side; only the shape of an entry is shared.

- [ ] **Step 1: Write the failing test**

Create `tests/test_env_vars.py`:

```python
from __future__ import annotations

import pytest

from remem.agents.base import Kind
from remem.agents.claude_code.adapter import ClaudeCodeAdapter
from remem.agents.claude_code.env_vars import CLAUDE_CODE_ENV_VARS


def test_the_adapter_exposes_its_env_table():
    table = ClaudeCodeAdapter().env_settings()
    assert table["BASH_DEFAULT_TIMEOUT_MS"].kind is Kind.INT
    assert table["BASH_DEFAULT_TIMEOUT_MS"].default == "120000"


def test_every_entry_carries_one_line_of_help():
    # The help string is what makes `remem config list` self-documenting,
    # which is the entire return on choosing a curated allowlist.
    for var in CLAUDE_CODE_ENV_VARS.values():
        assert var.help.strip()
        assert "\n" not in var.help


def test_the_table_keys_match_the_variable_names():
    for key, var in CLAUDE_CODE_ENV_VARS.items():
        assert key == var.name


@pytest.mark.parametrize(
    "forbidden",
    [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_VERTEX_BASE_URL",
    ],
)
def test_no_credential_or_endpoint_is_ever_settable(forbidden):
    # remem must never be the tool that writes a credential into a JSON file
    # on disk, and repointing an agent at another inference endpoint silently
    # exfiltrates prompts. Excluded by design, asserted so it stays that way.
    assert forbidden not in CLAUDE_CODE_ENV_VARS


def test_claude_config_dir_is_not_settable():
    # It names the file that would store it. Setting it inside settings.json
    # is a chicken-and-egg that reads as broken, and the Claude Code docs say
    # to set it in the shell.
    assert "CLAUDE_CONFIG_DIR" not in CLAUDE_CODE_ENV_VARS


def test_the_presence_only_toggles_are_marked_as_such():
    # Any non-empty value enables them, so `set DISABLE_TELEMETRY 0` would
    # disable telemetry - the opposite of how it reads. The kind is what lets
    # the service reject that.
    assert CLAUDE_CODE_ENV_VARS["DISABLE_TELEMETRY"].kind is Kind.PRESENCE
    assert CLAUDE_CODE_ENV_VARS["DISABLE_ERROR_REPORTING"].kind is Kind.PRESENCE


def test_autocompact_carries_its_one_way_note():
    var = CLAUDE_CODE_ENV_VARS["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"]
    assert var.note is not None
    assert "lower" in var.note
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_env_vars.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'remem.agents.claude_code.env_vars'`

- [ ] **Step 3: Write minimal implementation**

Create `src/remem/agents/claude_code/env_vars.py`:

```python
"""The Claude Code environment variables `remem config` may set.

This table lives on the adapter, not in services/, because
BASH_DEFAULT_TIMEOUT_MS is a fact about Claude Code rather than about remem.
Putting it in a service would make the service layer the place that knows
every agent's env schema - the exact coupling agents/registry.py exists to
prevent. A future adapter ships its own table and `remem config` works for it
with no service change. Only the *shape* of an entry is shared, and that
lives in agents/base.py so the service can validate without importing any
particular adapter.

Curated rather than passthrough: the help strings make `remem config list`
self-documenting, and two entries below encode traps that a passthrough
writer would let the user walk straight into.

Values and defaults transcribed from https://code.claude.com/docs/en/env-vars.
"""

from __future__ import annotations

from typing import Mapping

from remem.agents.base import EnvVar, Kind


def _var(name: str, kind: Kind, help: str, **kw) -> tuple[str, EnvVar]:
    return name, EnvVar(name=name, kind=kind, help=help, **kw)


CLAUDE_CODE_ENV_VARS: Mapping[str, EnvVar] = dict(
    [
        _var(
            "BASH_DEFAULT_TIMEOUT_MS",
            Kind.INT,
            "Default timeout for a bash command.",
            minimum=1,
            default="120000",
            duration=True,
        ),
        _var(
            "BASH_MAX_TIMEOUT_MS",
            Kind.INT,
            "Longest timeout the model may set on a bash command.",
            minimum=1,
            default="600000",
            duration=True,
        ),
        _var(
            "BASH_MAX_OUTPUT_LENGTH",
            Kind.INT,
            "Characters of bash output read back into context.",
            minimum=1,
            maximum=150000,
            default="30000",
        ),
        _var(
            "API_TIMEOUT_MS",
            Kind.INT,
            "Timeout for a single API request.",
            minimum=1,
            default="600000",
            duration=True,
        ),
        _var(
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
            Kind.INT,
            "Percentage of the context window that triggers auto-compaction.",
            minimum=1,
            maximum=100,
            note=(
                "Can only lower the built-in threshold, never raise it - a "
                "high value is accepted and does nothing."
            ),
        ),
        _var(
            "CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS",
            Kind.INT,
            "Stall timeout for a background subagent.",
            minimum=1,
            default="600000",
            duration=True,
        ),
        _var(
            "CLAUDE_AFK_TIMEOUT_MS",
            Kind.INT,
            "Auto-continue timeout for an unanswered question dialog.",
            minimum=0,
            duration=True,
        ),
        _var(
            "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR",
            Kind.BOOL,
            "Return to the project directory after every bash command.",
        ),
        # Presence-only. Any non-empty value enables the disabling, so "0"
        # would *disable telemetry* rather than re-enable it. Kind.PRESENCE is
        # what lets the service refuse "0" and point at `unset` instead.
        _var(
            "DISABLE_TELEMETRY",
            Kind.PRESENCE,
            "Disable telemetry collection.",
            note="Presence-only: unset it to re-enable, do not set it to 0.",
        ),
        _var(
            "DISABLE_ERROR_REPORTING",
            Kind.PRESENCE,
            "Disable error reporting.",
            note="Presence-only: unset it to re-enable, do not set it to 0.",
        ),
    ]
)

# Deliberately absent, and this comment is the reason they must stay absent:
#
#   ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_AWS_API_KEY,
#   ANTHROPIC_FOUNDRY_API_KEY, AWS_BEARER_TOKEN_BEDROCK and every other
#   credential - remem must never be the tool that writes a secret into a
#   JSON file on disk.
#
#   ANTHROPIC_BASE_URL and the whole *_BASE_URL family - repointing an agent
#   at a different inference endpoint is not a knowledge-store concern, and
#   getting it wrong silently sends prompts somewhere else.
#
#   CLAUDE_CONFIG_DIR - it names the file that would store it.
#
# tests/test_env_vars.py asserts each of these stays out.
```

Add to `ClaudeCodeAdapter` in `adapter.py`:

```python
    def env_settings(self) -> Mapping[str, EnvVar]:
        """The environment variables `remem config` may write for this agent.

        Data, not policy: routing, validation and precedence all live in
        services/settings.py. The adapter only answers what exists.
        """
        return CLAUDE_CODE_ENV_VARS
```

with `from remem.agents.claude_code.env_vars import CLAUDE_CODE_ENV_VARS, EnvVar` at the top.

In `src/remem/agents/base.py`, add the shared vocabulary (needs `from dataclasses import dataclass, field` - `dataclass` is already imported - plus `from enum import StrEnum`):

```python
class Kind(StrEnum):
    """How a setting's value is interpreted."""

    INT = "int"
    BOOL = "bool"
    #: Enabled by *presence*, whatever the value - so "0" enables it too.
    PRESENCE = "presence"
    STR = "str"


@dataclass(frozen=True, slots=True)
class EnvVar:
    """One settable environment variable, as an adapter declares it.

    Lives here rather than beside any particular table because
    services/settings.py validates against it, and a service that imported
    agents.claude_code would invert the pluggability seam.
    """

    name: str
    kind: Kind
    #: One line. Rendered by `remem config list`, so it must fit on a line.
    help: str
    minimum: int | None = None
    maximum: int | None = None
    #: The documented default, shown when the key is unset. A string because
    #: that is what lands in settings.json and what the user typed.
    default: str | None = None
    #: Accepts 10m / 30s / 500ms as well as a raw millisecond integer. A
    #: six-digit millisecond literal invites an off-by-one-zero.
    duration: bool = False
    #: Surfaced by `list` for a variable that does not behave as it reads.
    note: str | None = None
```

Then document the capability without adding it to the Protocol as a required method:

```python
class AgentAdapter(Protocol):
    name: str

    # env is injected rather than read from os.environ inside the adapter so
    # that tests can relocate an install without mutating the real process
    # environment - the same reason config.load() takes it.
    def install(
        self,
        scope: str,
        home: Path,
        env: Mapping[str, str] | None = None,
    ) -> InstallReport: ...
    def identity(self, env: Mapping[str, str], payload: dict) -> Identity: ...

    # Optional capability, probed with getattr rather than declared here:
    #
    #     def env_settings(self) -> Mapping[str, EnvVar]: ...
    #
    # Adapters written before `remem config` existed - including any third
    # party one already shipped - do not have it, and requiring it would break
    # them at import time. The registry contract is that a broken third-party
    # adapter warns rather than breaking remem, so the service probes instead.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_env_vars.py -v`
Expected: PASS (12 tests, counting the parametrised cases)

- [ ] **Step 5: Commit**

```bash
git add src/remem/agents/claude_code/env_vars.py src/remem/agents/claude_code/adapter.py src/remem/agents/base.py tests/test_env_vars.py
git commit -m "Add the Claude Code env var table to the adapter"
```

---

### Task 3: Value parsing and validation

Pure functions. No files touched, no routing yet - this is the piece Task 4 and Task 5 both call before writing anything.

**Files:**
- Create: `src/remem/services/settings.py`
- Test: `tests/test_settings_values.py`

**Interfaces:**
- Consumes: `EnvVar`, `Kind` from Task 2.
- Produces:
  - `class InvalidValue(ValueError)`
  - `parse_duration(raw: str) -> int` - `"10m"` -> `600000`; raises `InvalidValue` on a bad suffix.
  - `coerce(var: EnvVar, raw: str) -> str` - validates `raw` against `var` and returns the string to write. Raises `InvalidValue`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_settings_values.py`:

```python
from __future__ import annotations

import pytest

from remem.agents.claude_code.env_vars import CLAUDE_CODE_ENV_VARS
from remem.services.settings import InvalidValue, coerce, parse_duration


@pytest.mark.parametrize(
    "raw,expected",
    [("10m", 600000), ("30s", 30000), ("500ms", 500), ("600000", 600000)],
)
def test_parse_duration_accepts_suffixes_and_plain_milliseconds(raw, expected):
    assert parse_duration(raw) == expected


def test_parse_duration_rejects_an_unknown_suffix():
    with pytest.raises(InvalidValue):
        parse_duration("10h")


def test_a_duration_key_accepts_a_suffix():
    var = CLAUDE_CODE_ENV_VARS["BASH_DEFAULT_TIMEOUT_MS"]
    assert coerce(var, "10m") == "600000"


def test_a_non_duration_key_rejects_a_suffix():
    # BASH_MAX_OUTPUT_LENGTH counts characters, not milliseconds, so "10m"
    # is a mistake rather than shorthand.
    var = CLAUDE_CODE_ENV_VARS["BASH_MAX_OUTPUT_LENGTH"]
    with pytest.raises(InvalidValue):
        coerce(var, "10m")


def test_an_int_below_the_minimum_is_refused():
    var = CLAUDE_CODE_ENV_VARS["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"]
    with pytest.raises(InvalidValue) as exc:
        coerce(var, "0")
    assert "1" in str(exc.value) and "100" in str(exc.value)


def test_an_int_above_the_maximum_is_refused():
    var = CLAUDE_CODE_ENV_VARS["BASH_MAX_OUTPUT_LENGTH"]
    with pytest.raises(InvalidValue):
        coerce(var, "150001")


def test_a_presence_key_refuses_zero_and_points_at_unset():
    # The trap: any non-empty value enables the disabling, so "0" would
    # disable telemetry rather than re-enable it.
    var = CLAUDE_CODE_ENV_VARS["DISABLE_TELEMETRY"]
    with pytest.raises(InvalidValue) as exc:
        coerce(var, "0")
    assert "unset" in str(exc.value)


def test_a_presence_key_accepts_a_truthy_value():
    var = CLAUDE_CODE_ENV_VARS["DISABLE_TELEMETRY"]
    assert coerce(var, "1") == "1"


def test_a_bool_key_normalises_true_and_false():
    var = CLAUDE_CODE_ENV_VARS["CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR"]
    assert coerce(var, "true") == "1"
    assert coerce(var, "0") == "0"


def test_an_empty_string_is_always_allowed():
    # Claude Code's documented way to neutralise a shell variable the user
    # does not control: "VAR": "" in the settings file.
    var = CLAUDE_CODE_ENV_VARS["BASH_DEFAULT_TIMEOUT_MS"]
    assert coerce(var, "") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_values.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'remem.services.settings'`

- [ ] **Step 3: Write minimal implementation**

Create `src/remem/services/settings.py`:

```python
"""Reads and writes remem's own settings and an agent's environment block.

Every policy decision for `remem config` lives here: which file a key belongs
to, whether a value is legal, and whether a write will actually take effect.
The adapter supplies the table of what exists; this module decides what may
be done with it.

The one service that touches no store - `remem config` must work with
Postgres down.
"""

from __future__ import annotations

import re

from remem.agents.base import EnvVar, Kind


class InvalidValue(ValueError):
    """The value is not legal for this setting. Nothing has been written."""


_DURATION = re.compile(r"^(\d+)(ms|s|m)$")
_MULTIPLIER = {"ms": 1, "s": 1000, "m": 60000}

# "0"/"false" on a Kind.BOOL key is meaningful; on a Kind.PRESENCE key it is
# a trap, because Claude Code enables those on any non-empty value.
_FALSEY = {"0", "false", "no", "off"}
_TRUTHY = {"1", "true", "yes", "on"}


def parse_duration(raw: str) -> int:
    """Milliseconds from 10m / 30s / 500ms, or from a plain integer."""
    if raw.isdigit():
        return int(raw)
    match = _DURATION.match(raw.strip().lower())
    if not match:
        raise InvalidValue(
            f"{raw!r} is not a duration. Use milliseconds, or a suffix: "
            "500ms, 30s, 10m."
        )
    return int(match.group(1)) * _MULTIPLIER[match.group(2)]


def coerce(var: EnvVar, raw: str) -> str:
    """Validate raw against var and return the string to write.

    Raises InvalidValue rather than writing something approximate. Callers
    must call this before touching a file, so that a rejected value leaves
    the file byte-identical.
    """
    # An empty string is never a type error: it is Claude Code's documented
    # way to override a shell variable the user cannot otherwise control.
    # `unset` is the separate operation that removes the key.
    if raw == "":
        return ""

    if var.kind is Kind.PRESENCE:
        if raw.strip().lower() in _FALSEY:
            raise InvalidValue(
                f"{var.name} is enabled by presence, so {raw!r} would still "
                f"enable it. Use `remem config unset {var.name}` instead."
            )
        return raw

    if var.kind is Kind.BOOL:
        value = raw.strip().lower()
        if value in _TRUTHY:
            return "1"
        if value in _FALSEY:
            return "0"
        raise InvalidValue(f"{var.name} takes a boolean, not {raw!r}.")

    if var.kind is Kind.INT:
        if var.duration:
            number = parse_duration(raw)
        elif raw.isdigit():
            number = int(raw)
        else:
            raise InvalidValue(f"{var.name} takes a whole number, not {raw!r}.")
        if var.minimum is not None and number < var.minimum:
            raise InvalidValue(
                f"{var.name} must be between {var.minimum} and "
                f"{var.maximum if var.maximum is not None else 'unbounded'}; "
                f"got {number}."
            )
        if var.maximum is not None and number > var.maximum:
            raise InvalidValue(
                f"{var.name} must be between "
                f"{var.minimum if var.minimum is not None else 'unbounded'} "
                f"and {var.maximum}; got {number}."
            )
        return str(number)

    return raw
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_values.py -v`
Expected: PASS (14 tests, counting the parametrised cases)

Note: `test_an_int_below_the_minimum_is_refused` asserts both `"1"` and `"100"` appear in the message, which the minimum branch above satisfies because it prints both bounds.

- [ ] **Step 5: Commit**

```bash
git add src/remem/services/settings.py tests/test_settings_values.py
git commit -m "Validate and coerce config values before any write"
```

---

### Task 4: Routing and the remem-side settings

Decide which file a key belongs to, and read/write remem's own `config.toml`.

**Files:**
- Modify: `src/remem/services/settings.py`
- Modify: `pyproject.toml` (add `tomli-w`)
- Test: `tests/test_settings_routing.py`

**Interfaces:**
- Consumes: `coerce`, `InvalidValue` from Task 3.
- Produces:
  - `class UnknownSetting(KeyError)`
  - `class NotSettable(ValueError)`
  - `REMEM_VARS: dict[str, EnvVar]` - remem's own settings, derived from `config.py`'s `DEFAULT_*` constants.
  - `Target` - a `StrEnum` with `REMEM` and `AGENT`.
  - `route(key: str, table: Mapping[str, EnvVar]) -> tuple[Target, EnvVar]` - raises `UnknownSetting` or `NotSettable`.
  - `file_key(key: str) -> str` - `"REMEM_MAX_CHARS"` -> `"max_chars"`.
  - `write_remem(path: Path, key: str, value: str | None) -> None` - `value=None` unsets.

- [ ] **Step 1: Add the dependency**

```bash
uv add tomli-w
```

Expected: `pyproject.toml` gains `"tomli-w>=1.0"` under `dependencies`, and `uv.lock` updates.

- [ ] **Step 2: Write the failing test**

Create `tests/test_settings_routing.py`:

```python
from __future__ import annotations

import tomllib

import pytest

from remem.agents.claude_code.env_vars import CLAUDE_CODE_ENV_VARS
from remem.services.settings import (
    NotSettable,
    Target,
    UnknownSetting,
    file_key,
    route,
    write_remem,
)

TABLE = CLAUDE_CODE_ENV_VARS


def test_a_remem_key_routes_to_remems_own_config():
    target, var = route("REMEM_MAX_CHARS", TABLE)
    assert target is Target.REMEM
    assert var.name == "REMEM_MAX_CHARS"


def test_the_unprefixed_spelling_routes_the_same_way():
    # remem's file keys are unprefixed while its documentation is written in
    # env-var spelling. Both must work as input.
    assert route("max_chars", TABLE)[0] is Target.REMEM


def test_both_spellings_reach_the_same_file_key():
    assert file_key("REMEM_MAX_CHARS") == "max_chars"
    assert file_key("max_chars") == "max_chars"


def test_a_claude_code_key_routes_to_the_agent():
    target, var = route("BASH_DEFAULT_TIMEOUT_MS", TABLE)
    assert target is Target.AGENT
    assert var.name == "BASH_DEFAULT_TIMEOUT_MS"


def test_an_unknown_key_is_refused_and_names_what_is_supported():
    with pytest.raises(UnknownSetting) as exc:
        route("NONSENSE_KEY", TABLE)
    message = str(exc.value)
    assert "NONSENSE_KEY" in message
    assert "BASH_DEFAULT_TIMEOUT_MS" in message
    assert "REMEM_MAX_CHARS" in message


@pytest.mark.parametrize("key", ["CLAUDE_CONFIG_DIR", "REMEM_CONFIG"])
def test_the_two_bootstrap_variables_are_refused(key):
    # Each names the file that would store it. The error has to send the
    # user to the shell rather than leave them looking for a typo.
    with pytest.raises(NotSettable) as exc:
        route(key, TABLE)
    assert "shell" in str(exc.value)


def test_write_remem_creates_the_file_with_the_unprefixed_key(tmp_path):
    path = tmp_path / "config.toml"
    write_remem(path, "REMEM_MAX_CHARS", "8000")
    assert tomllib.loads(path.read_text())["max_chars"] == 8000


def test_write_remem_preserves_other_settings(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('capture_model = "opus"\n')
    write_remem(path, "REMEM_MAX_CHARS", "8000")
    data = tomllib.loads(path.read_text())
    assert data["capture_model"] == "opus"
    assert data["max_chars"] == 8000


def test_write_remem_round_trips_a_dsn_with_quotes_and_backslashes(tmp_path):
    # The case that justified taking tomli-w as a dependency rather than
    # hand-rolling a writer: TOML string escaping is not worth being clever
    # about when a password can contain anything.
    path = tmp_path / "config.toml"
    nasty = r'postgresql://u:pa"ss\word@localhost:5433/remem'
    write_remem(path, "REMEM_DSN", nasty)
    assert tomllib.loads(path.read_text())["dsn"] == nasty


def test_write_remem_backs_up_before_rewriting(tmp_path):
    # A rewrite loses comments and formatting, so the previous file has to
    # survive somewhere.
    path = tmp_path / "config.toml"
    path.write_text("# hand written\ncapture_model = 'opus'\n")
    write_remem(path, "REMEM_MAX_CHARS", "8000")
    backups = list(tmp_path.glob("config.toml.bak*"))
    assert len(backups) == 1
    assert "# hand written" in backups[0].read_text()


def test_write_remem_unsets_with_none(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("max_chars = 8000\ncapture_model = 'opus'\n")
    write_remem(path, "REMEM_MAX_CHARS", None)
    data = tomllib.loads(path.read_text())
    assert "max_chars" not in data
    assert data["capture_model"] == "opus"


def test_int_settings_are_written_as_toml_integers(tmp_path):
    # Written as a string, config.load()'s int() would still work, but
    # `remem config get` would round-trip 8000 as "8000" and the file would
    # not match what a human would have written.
    path = tmp_path / "config.toml"
    write_remem(path, "REMEM_MAX_CHARS", "8000")
    assert "max_chars = 8000" in path.read_text()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_routing.py -v`
Expected: FAIL with `ImportError: cannot import name 'route' from 'remem.services.settings'`

- [ ] **Step 4: Write minimal implementation**

Append to `src/remem/services/settings.py`:

```python
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Mapping

import tomli_w

from remem import config as remem_config
from remem import jsonfile


class UnknownSetting(KeyError):
    """No such setting in either table."""


class NotSettable(ValueError):
    """A real variable that must not be written into a settings file."""


class Target(StrEnum):
    REMEM = "remem"
    AGENT = "agent"


# Each of these names the file that would store it, so writing one inside
# that file is a chicken-and-egg that reads as broken. The Claude Code docs
# say the same about CLAUDE_CONFIG_DIR: set it in the shell.
BOOTSTRAP_VARS = {"REMEM_CONFIG", "CLAUDE_CONFIG_DIR"}

#: remem's own settings, keyed by env-var spelling. Derived from config.py so
#: that a new setting there shows up in `remem config list` without a second
#: edit here; only the help text lives in this table.
REMEM_VARS: Mapping[str, EnvVar] = {
    "REMEM_DSN": EnvVar(
        "REMEM_DSN", Kind.STR, "Postgres connection string.",
        default=remem_config.DEFAULT_DSN,
    ),
    "REMEM_USER_ID": EnvVar(
        "REMEM_USER_ID", Kind.STR, "Handle entries are attributed to.",
        default=remem_config.DEFAULT_HANDLE,
    ),
    "REMEM_MAX_CHARS": EnvVar(
        "REMEM_MAX_CHARS", Kind.INT, "Cap on a rendered context block.",
        minimum=1, default=str(remem_config.DEFAULT_MAX_CHARS),
    ),
    "REMEM_FUZZY_THRESHOLD": EnvVar(
        "REMEM_FUZZY_THRESHOLD", Kind.STR,
        "Trigram similarity floor for the fuzzy fallback (0 < t <= 1).",
        default=str(remem_config.DEFAULT_FUZZY_THRESHOLD),
    ),
    "REMEM_CAPTURE_MODEL": EnvVar(
        "REMEM_CAPTURE_MODEL", Kind.STR,
        "Model used to distil captured sessions.",
        default=remem_config.DEFAULT_CAPTURE_MODEL,
    ),
    "REMEM_TURN_WARN_AT": EnvVar(
        "REMEM_TURN_WARN_AT", Kind.INT,
        "Turn count at which the handoff reminder first fires.",
        minimum=1, default=str(remem_config.DEFAULT_TURN_WARN_AT),
    ),
    "REMEM_TURN_WARN_EVERY": EnvVar(
        "REMEM_TURN_WARN_EVERY", Kind.INT,
        "Turns between repeat handoff reminders.",
        minimum=1, default=str(remem_config.DEFAULT_TURN_WARN_EVERY),
    ),
}

#: config.toml uses unprefixed keys; user_handle is the one that is not just
#: the lowercased suffix.
_FILE_KEYS = {name: name.removeprefix("REMEM_").lower() for name in REMEM_VARS}
_FILE_KEYS["REMEM_USER_ID"] = "user_handle"
_BY_FILE_KEY = {v: k for k, v in _FILE_KEYS.items()}


def file_key(key: str) -> str:
    """The config.toml spelling of a remem setting, from either input form."""
    if key in _FILE_KEYS:
        return _FILE_KEYS[key]
    if key in _BY_FILE_KEY:
        return key
    raise UnknownSetting(key)


def route(key: str, table: Mapping[str, EnvVar]) -> tuple[Target, EnvVar]:
    """Which file this key belongs to, and its definition.

    Refuses rather than guessing: an unrecognised key is far more often a
    typo than a variable remem has not heard of, and silently writing it
    would leave a dead entry that looks like a working setting.
    """
    if key in BOOTSTRAP_VARS:
        raise NotSettable(
            f"{key} names the file that would store it, so it cannot be set "
            "there. Export it from your shell instead."
        )
    if key in REMEM_VARS:
        return Target.REMEM, REMEM_VARS[key]
    if key in _BY_FILE_KEY:
        return Target.REMEM, REMEM_VARS[_BY_FILE_KEY[key]]
    if key in table:
        return Target.AGENT, table[key]
    supported = ", ".join(sorted(set(REMEM_VARS) | set(table)))
    raise UnknownSetting(f"unknown setting '{key}'. Supported: {supported}")


def write_remem(path: Path, key: str, value: str | None) -> None:
    """Set or unset one key in remem's config.toml.

    Rewriting the file loses comments and formatting, which is why it is
    backed up first. tomli-w rather than a hand-rolled writer because the DSN
    can hold a password containing quotes or backslashes, and TOML escaping
    is the wrong thing to be clever about.
    """
    data: dict = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError:
            # Same posture as config.load(): a broken file must not be a
            # dead end. It is backed up below before being replaced.
            data = {}
        jsonfile.backup_once(path, set())

    name = file_key(key)
    if value is None:
        data.pop(name, None)
    else:
        env_key = _BY_FILE_KEY.get(name, name)
        var = REMEM_VARS[env_key]
        # Write the natural TOML type so the file reads the way a human
        # would have written it, and `get` round-trips what was set.
        data[name] = int(value) if var.kind is Kind.INT and value else value

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(data))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_routing.py -v`
Expected: PASS (13 tests, counting the parametrised cases)

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, 0 skipped.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/remem/services/settings.py tests/test_settings_routing.py
git commit -m "Route config keys to the right file and write config.toml"
```

---

### Task 5: The agent-side write, and the precedence report

The two remaining policy pieces: writing the `env` block of `settings.json`, and telling the user whether the value they just set will actually take effect.

**Files:**
- Modify: `src/remem/services/settings.py`
- Test: `tests/test_settings_precedence.py`

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces:
  - `@dataclass(frozen=True, slots=True) class Setting` with fields `key: str`, `value: str | None`, `source: str`, `var: EnvVar`, `target: Target`
  - `write_agent(path: Path, key: str, value: str | None) -> None`
  - `shadow_warning(target: Target, key: str, env: Mapping[str, str]) -> str | None`
  - `list_settings(remem_path: Path, agent_path: Path, table: Mapping[str, EnvVar], env: Mapping[str, str]) -> list[Setting]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_settings_precedence.py`:

```python
from __future__ import annotations

import json

from remem.agents.claude_code.env_vars import CLAUDE_CODE_ENV_VARS
from remem.services.settings import (
    Target,
    list_settings,
    shadow_warning,
    write_agent,
    write_remem,
)

TABLE = CLAUDE_CODE_ENV_VARS


def test_write_agent_creates_the_env_block(tmp_path):
    path = tmp_path / "settings.json"
    write_agent(path, "BASH_DEFAULT_TIMEOUT_MS", "600000")
    assert json.loads(path.read_text())["env"] == {
        "BASH_DEFAULT_TIMEOUT_MS": "600000"
    }


def test_write_agent_preserves_unrelated_settings(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {"SessionStart": []}, "env": {"X": "1"}}))
    write_agent(path, "BASH_DEFAULT_TIMEOUT_MS", "600000")
    data = json.loads(path.read_text())
    assert data["hooks"] == {"SessionStart": []}
    assert data["env"]["X"] == "1"
    assert data["env"]["BASH_DEFAULT_TIMEOUT_MS"] == "600000"


def test_write_agent_backs_up_before_touching_an_existing_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {}}))
    write_agent(path, "BASH_DEFAULT_TIMEOUT_MS", "600000")
    assert len(list(tmp_path.glob("settings.json.bak*"))) == 1


def test_write_agent_unsets_with_none(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"env": {"BASH_DEFAULT_TIMEOUT_MS": "1", "X": "2"}}))
    write_agent(path, "BASH_DEFAULT_TIMEOUT_MS", None)
    env = json.loads(path.read_text())["env"]
    assert "BASH_DEFAULT_TIMEOUT_MS" not in env
    assert env["X"] == "2"


def test_an_empty_string_is_written_not_removed(tmp_path):
    # Distinct from unset: "" is Claude Code's documented way to neutralise a
    # shell variable the user does not control.
    path = tmp_path / "settings.json"
    write_agent(path, "BASH_DEFAULT_TIMEOUT_MS", "")
    assert json.loads(path.read_text())["env"]["BASH_DEFAULT_TIMEOUT_MS"] == ""


def test_a_remem_key_warns_when_an_export_shadows_the_file():
    # For remem, the environment beats config.toml, so writing the file while
    # the variable is exported is a silent no-op.
    warning = shadow_warning(Target.REMEM, "REMEM_MAX_CHARS", {"REMEM_MAX_CHARS": "1"})
    assert warning is not None
    assert "will not take effect" in warning


def test_a_remem_key_is_quiet_when_nothing_shadows_it():
    assert shadow_warning(Target.REMEM, "REMEM_MAX_CHARS", {}) is None


def test_an_agent_key_notes_that_it_overrides_the_export():
    # The opposite direction: settings.json beats a shell export.
    warning = shadow_warning(
        Target.AGENT, "BASH_DEFAULT_TIMEOUT_MS", {"BASH_DEFAULT_TIMEOUT_MS": "1"}
    )
    assert warning is not None
    assert "overrides" in warning


def test_an_agent_key_is_quiet_when_nothing_is_exported():
    assert shadow_warning(Target.AGENT, "BASH_DEFAULT_TIMEOUT_MS", {}) is None


def test_list_reports_the_default_when_nothing_is_set(tmp_path):
    settings = list_settings(
        tmp_path / "config.toml", tmp_path / "settings.json", TABLE, {}
    )
    row = next(s for s in settings if s.key == "BASH_DEFAULT_TIMEOUT_MS")
    assert row.value == "120000"
    assert row.source == "default"


def test_list_reports_the_file_as_the_source(tmp_path):
    remem_path = tmp_path / "config.toml"
    write_remem(remem_path, "REMEM_MAX_CHARS", "8000")
    settings = list_settings(remem_path, tmp_path / "settings.json", TABLE, {})
    row = next(s for s in settings if s.key == "REMEM_MAX_CHARS")
    assert row.value == "8000"
    assert row.source == "file"


def test_list_reports_the_environment_winning_for_a_remem_key(tmp_path):
    remem_path = tmp_path / "config.toml"
    write_remem(remem_path, "REMEM_MAX_CHARS", "8000")
    settings = list_settings(
        remem_path, tmp_path / "settings.json", TABLE, {"REMEM_MAX_CHARS": "999"}
    )
    row = next(s for s in settings if s.key == "REMEM_MAX_CHARS")
    assert row.value == "999"
    assert row.source == "environment"


def test_list_reports_the_file_winning_for_an_agent_key(tmp_path):
    # The asymmetry: for Claude Code the file beats the export.
    agent_path = tmp_path / "settings.json"
    write_agent(agent_path, "BASH_DEFAULT_TIMEOUT_MS", "600000")
    settings = list_settings(
        tmp_path / "config.toml",
        agent_path,
        TABLE,
        {"BASH_DEFAULT_TIMEOUT_MS": "999"},
    )
    row = next(s for s in settings if s.key == "BASH_DEFAULT_TIMEOUT_MS")
    assert row.value == "600000"
    assert row.source == "file"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_precedence.py -v`
Expected: FAIL with `ImportError: cannot import name 'write_agent' from 'remem.services.settings'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/remem/services/settings.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Setting:
    key: str
    value: str | None
    #: "environment", "file", or "default" - the whole point of `list`.
    source: str
    var: EnvVar
    target: Target


def write_agent(path: Path, key: str, value: str | None) -> None:
    """Set or unset one key in the env block of an agent's settings.json."""
    backed_up: set[Path] = set()
    data, _ = jsonfile.read_json(path, backed_up)
    env_block = data.setdefault("env", {})
    if value is None:
        env_block.pop(key, None)
    else:
        env_block[key] = value
    jsonfile.write_json(path, data, backed_up)


def shadow_warning(
    target: Target, key: str, env: Mapping[str, str]
) -> str | None:
    """Whether the value just written will actually be the one in effect.

    The two targets resolve in opposite directions, which is the single most
    confusing thing about this command:

        remem        environment beats config.toml
        Claude Code  settings.json beats the environment

    So the same situation - the key is also exported - is a silent no-op on
    one side and the intended behaviour on the other. Saying nothing would
    leave the user staring at a correctly written file that changed nothing.
    """
    if key not in env:
        return None
    if target is Target.REMEM:
        return (
            f"{key} is set in your environment, which takes precedence over "
            f"the config file, so this change will not take effect until you "
            f"unset it."
        )
    return (
        f"{key} is also set in your environment. The settings file overrides "
        f"it, so the value just written is the one that will be used."
    )


def _agent_env(path: Path) -> dict:
    data, _ = jsonfile.read_json(path, set())
    block = data.get("env")
    return block if isinstance(block, dict) else {}


def list_settings(
    remem_path: Path,
    agent_path: Path,
    table: Mapping[str, EnvVar],
    env: Mapping[str, str],
) -> list[Setting]:
    """Every settable key, its effective value, and where that value came from.

    This is the feature that justifies the command existing alongside
    Claude Code's own /config: one view over both tools, with each side's
    precedence rule already applied.
    """
    rows: list[Setting] = []

    file_data: dict = {}
    if remem_path.exists():
        try:
            file_data = tomllib.loads(remem_path.read_text())
        except tomllib.TOMLDecodeError:
            file_data = {}

    for key, var in REMEM_VARS.items():
        # Environment first: config.load() picks the env var over the file.
        if key in env:
            rows.append(Setting(key, env[key], "environment", var, Target.REMEM))
            continue
        name = _FILE_KEYS[key]
        if name in file_data:
            rows.append(
                Setting(key, str(file_data[name]), "file", var, Target.REMEM)
            )
            continue
        rows.append(Setting(key, var.default, "default", var, Target.REMEM))

    agent_block = _agent_env(agent_path)
    for key, var in table.items():
        # File first: for Claude Code the settings file beats the export.
        if key in agent_block:
            rows.append(
                Setting(key, agent_block[key], "file", var, Target.AGENT)
            )
            continue
        if key in env:
            rows.append(
                Setting(key, env[key], "environment", var, Target.AGENT)
            )
            continue
        rows.append(Setting(key, var.default, "default", var, Target.AGENT))

    return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_precedence.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add src/remem/services/settings.py tests/test_settings_precedence.py
git commit -m "Write the agent env block and report value precedence"
```

---

### Task 6: The CLI command group

Parsing and formatting only. Every decision was made in Tasks 3-5.

**Files:**
- Modify: `src/remem/cli.py`
- Test: `tests/test_config_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 2-5, plus `resolve_paths` from `agents/claude_code/adapter.py`.
- Produces: the `remem config` command group. No new importable names.

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_cli.py`:

```python
from __future__ import annotations

import json
import tomllib

from typer.testing import CliRunner

from remem.cli import app

runner = CliRunner()


def _env(tmp_path, **extra):
    """Point both targets at a temp dir and keep the real home untouched."""
    return {
        "REMEM_CONFIG": str(tmp_path / "config.toml"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
        **extra,
    }


def test_set_writes_a_claude_code_key(tmp_path):
    result = runner.invoke(
        app,
        ["config", "set", "BASH_DEFAULT_TIMEOUT_MS", "10m"],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    settings = json.loads((tmp_path / "claude" / "settings.json").read_text())
    assert settings["env"]["BASH_DEFAULT_TIMEOUT_MS"] == "600000"
    # The resolved value is echoed so 10m is visibly 600000.
    assert "600000" in result.stdout


def test_set_writes_a_remem_key(tmp_path):
    result = runner.invoke(
        app, ["config", "set", "REMEM_MAX_CHARS", "8000"], env=_env(tmp_path)
    )
    assert result.exit_code == 0
    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert data["max_chars"] == 8000


def test_set_rejects_an_unknown_key_with_a_nonzero_exit(tmp_path):
    result = runner.invoke(
        app, ["config", "set", "NOPE", "1"], env=_env(tmp_path)
    )
    assert result.exit_code == 1
    assert "NOPE" in result.output


def test_set_leaves_the_file_untouched_when_the_value_is_refused(tmp_path):
    path = tmp_path / "claude" / "settings.json"
    runner.invoke(
        app,
        ["config", "set", "BASH_MAX_OUTPUT_LENGTH", "30000"],
        env=_env(tmp_path),
    )
    before = path.read_bytes()

    result = runner.invoke(
        app,
        ["config", "set", "BASH_MAX_OUTPUT_LENGTH", "999999"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    assert path.read_bytes() == before


def test_set_warns_when_an_export_shadows_a_remem_key(tmp_path):
    result = runner.invoke(
        app,
        ["config", "set", "REMEM_MAX_CHARS", "8000"],
        env=_env(tmp_path, REMEM_MAX_CHARS="999"),
    )
    assert result.exit_code == 0
    assert "will not take effect" in result.output


def test_get_prints_the_effective_value(tmp_path):
    runner.invoke(
        app, ["config", "set", "REMEM_MAX_CHARS", "8000"], env=_env(tmp_path)
    )
    result = runner.invoke(
        app, ["config", "get", "REMEM_MAX_CHARS"], env=_env(tmp_path)
    )
    assert result.exit_code == 0
    assert "8000" in result.stdout


def test_unset_removes_the_key(tmp_path):
    runner.invoke(
        app,
        ["config", "set", "BASH_DEFAULT_TIMEOUT_MS", "600000"],
        env=_env(tmp_path),
    )
    result = runner.invoke(
        app, ["config", "unset", "BASH_DEFAULT_TIMEOUT_MS"], env=_env(tmp_path)
    )
    assert result.exit_code == 0
    settings = json.loads((tmp_path / "claude" / "settings.json").read_text())
    assert "BASH_DEFAULT_TIMEOUT_MS" not in settings["env"]


def test_list_shows_keys_values_and_sources(tmp_path):
    runner.invoke(
        app, ["config", "set", "REMEM_MAX_CHARS", "8000"], env=_env(tmp_path)
    )
    result = runner.invoke(app, ["config", "list"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "REMEM_MAX_CHARS" in result.stdout
    assert "8000" in result.stdout
    assert "file" in result.stdout
    assert "BASH_DEFAULT_TIMEOUT_MS" in result.stdout
    assert "default" in result.stdout


def test_list_surfaces_a_variables_note(tmp_path):
    result = runner.invoke(app, ["config", "list"], env=_env(tmp_path))
    assert "lower" in result.stdout


def test_an_unknown_agent_is_refused_cleanly(tmp_path):
    # Not a traceback: registry.get already names what is registered.
    result = runner.invoke(
        app, ["config", "list", "--agent", "nope"], env=_env(tmp_path)
    )
    assert result.exit_code == 1
    assert "nope" in result.output
    assert "claude-code" in result.output


def test_config_works_with_postgres_unreachable(tmp_path):
    # No command in this group may open a session. A bad DSN must not matter.
    result = runner.invoke(
        app,
        ["config", "list"],
        env=_env(tmp_path, REMEM_DSN="postgresql://nobody@127.0.0.1:1/none"),
    )
    assert result.exit_code == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_cli.py -v`
Expected: FAIL - every test exits 2 with "No such command 'config'".

- [ ] **Step 3: Write minimal implementation**

Add to `src/remem/cli.py`, following the existing lazy-import style used by `install` and the `capture` group:

```python
config_app = typer.Typer(help="remem and agent settings.")
app.add_typer(config_app, name="config")


def _config_targets(agent: str):
    """The two files `remem config` reads and writes, plus the agent's table.

    CLAUDE_CONFIG_DIR is honoured through the adapter's own resolver, so this
    command lands in the same place `remem install` did.
    """
    import os
    from pathlib import Path

    from remem.agents.claude_code.adapter import resolve_paths
    from remem.agents.registry import UnknownAgent, get as get_adapter
    from remem.config import default_config_path

    env = os.environ
    try:
        adapter = get_adapter(agent)()
    except UnknownAgent as exc:
        # registry.get already lists what is registered, so echo it as-is.
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    paths = resolve_paths(Path.home(), env)
    remem_path = Path(env.get("REMEM_CONFIG", default_config_path()))
    table = getattr(adapter, "env_settings", lambda: {})()
    return env, remem_path, paths.settings, table


@config_app.command("set")
def config_set(
    key: str,
    value: str,
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
):
    """Set one setting, in remem's config or the agent's environment."""
    from remem.services import settings as svc

    env, remem_path, agent_path, table = _config_targets(agent)
    try:
        target, var = svc.route(key, table)
        resolved = svc.coerce(var, value)
    except (svc.UnknownSetting, svc.NotSettable, svc.InvalidValue) as exc:
        # KeyError stringifies with quotes around it; strip them so the
        # message reads like a sentence rather than a repr.
        typer.echo(str(exc).strip("\"'"), err=True)
        raise typer.Exit(1)

    if target is svc.Target.REMEM:
        svc.write_remem(remem_path, var.name, resolved)
        where = remem_path
    else:
        svc.write_agent(agent_path, var.name, resolved)
        where = agent_path

    typer.echo(f"{var.name} = {resolved!r} in {where}")
    warning = svc.shadow_warning(target, var.name, env)
    if warning:
        typer.echo(warning, err=True)


@config_app.command("unset")
def config_unset(
    key: str,
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
):
    """Remove one setting, restoring its default."""
    from remem.services import settings as svc

    _, remem_path, agent_path, table = _config_targets(agent)
    try:
        target, var = svc.route(key, table)
    except (svc.UnknownSetting, svc.NotSettable) as exc:
        typer.echo(str(exc).strip("\"'"), err=True)
        raise typer.Exit(1)

    if target is svc.Target.REMEM:
        svc.write_remem(remem_path, var.name, None)
    else:
        svc.write_agent(agent_path, var.name, None)
    typer.echo(f"Unset {var.name}.")


@config_app.command("get")
def config_get(
    key: str,
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
):
    """Print one setting's effective value and where it came from."""
    from remem.services import settings as svc

    env, remem_path, agent_path, table = _config_targets(agent)
    try:
        _, var = svc.route(key, table)
    except (svc.UnknownSetting, svc.NotSettable) as exc:
        typer.echo(str(exc).strip("\"'"), err=True)
        raise typer.Exit(1)

    rows = svc.list_settings(remem_path, agent_path, table, env)
    row = next(r for r in rows if r.key == var.name)
    typer.echo(f"{row.value if row.value is not None else '(unset)'}\t{row.source}")


@config_app.command("list")
def config_list(
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
):
    """Show every settable key, its value, and where that value came from."""
    from remem.services import settings as svc

    env, remem_path, agent_path, table = _config_targets(agent)
    if not table:
        typer.echo(f"{agent} has no settable environment variables.")
    for row in svc.list_settings(remem_path, agent_path, table, env):
        value = row.value if row.value is not None else "(unset)"
        typer.echo(f"{row.key}\t{value}\t{row.source}\t{row.var.help}")
        if row.var.note:
            typer.echo(f"\t{row.var.note}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config_cli.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, 0 skipped.

- [ ] **Step 6: Commit**

```bash
git add src/remem/cli.py tests/test_config_cli.py
git commit -m "Add the remem config command group"
```

---

### Task 7: Documentation

The feature is not shipped until the next reader can find it.

**Files:**
- Modify: `CLAUDE.md`
- Modify: `src/remem/agents/claude_code/skills/remem/SKILL.md`
- Modify: `src/remem/agents/claude_code/adapter.py` (install note)
- Test: `tests/test_claude_code_install.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_claude_code_install.py`:

```python
def test_install_mentions_the_config_command(tmp_path):
    # The install report is where someone learns what remem can do for them
    # next; a command nobody is pointed at is a command nobody runs.
    report = ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    assert any("remem config" in n for n in report.notes)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_claude_code_install.py::test_install_mentions_the_config_command -v`
Expected: FAIL with `assert False`

- [ ] **Step 3: Write minimal implementation**

In `adapter.py`, add a note constant and append it in `install()` beside the existing ones:

```python
CONFIG_NOTE = (
    "Tune remem and Claude Code together with `remem config list` - it shows "
    "every setting, its value, and whether that value came from the "
    "environment, a file, or a default."
)
```

In `CLAUDE.md`, add a section after "Handoffs":

```markdown
### Settings

`remem config` reads and writes two files from one command, routed by key
name: `REMEM_*` keys land in remem's `config.toml`, a curated set of Claude
Code environment variables lands in the `env` block of `settings.json`.

- The table of Claude Code variables lives on the **adapter**
  (`agents/claude_code/env_vars.py`), not in `services/`. It is a fact about
  Claude Code, not about remem, and keeping it there is what lets a future
  adapter ship its own.
- **No credential and no endpoint variable is ever settable.** Their absence
  from the table is the enforcement; `tests/test_env_vars.py` asserts it.
  Neither is `CLAUDE_CONFIG_DIR` or `REMEM_CONFIG` - each names the file that
  would store it.
- The two targets resolve in **opposite directions**: the environment beats
  remem's `config.toml`, while `settings.json` beats a shell export. `set`
  says so when the key it just wrote is also exported, because writing a
  shadowed remem key is otherwise a silent no-op.
- This is the one service that opens no database connection. It must keep
  working with Postgres down.
```

In `skills/remem/SKILL.md`, add before "## Knowledge bases":

```markdown
## Settings

`remem config list` shows every remem and Claude Code setting, its value, and
whether it came from the environment, a file, or a default.

```bash
remem config set BASH_DEFAULT_TIMEOUT_MS 10m
remem config unset DISABLE_TELEMETRY
```

If the user asks why a setting they changed had no effect, run `remem config
get <key>` - a `source` of `environment` on a remem key means an export is
shadowing the file.
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, 0 skipped.

- [ ] **Step 5: Verify against the installed binary**

The MCP server and hooks are registered as a bare `remem`, so a change that
only works in the project venv is not shipped. Check the real command:

```bash
uv tool install --editable .
remem config list
remem config set BASH_DEFAULT_TIMEOUT_MS 10m
remem config get BASH_DEFAULT_TIMEOUT_MS
remem config unset BASH_DEFAULT_TIMEOUT_MS
```

Expected: `list` prints every key with a source column; `set` echoes 600000;
`unset` removes it. Confirm `~/.claude/settings.json` still holds its hooks
and that a `.bak` file was written beside it.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md src/remem/agents/claude_code/skills/remem/SKILL.md src/remem/agents/claude_code/adapter.py tests/test_claude_code_install.py
git commit -m "Document the config command"
```

---

## Notes for the executor

- `tests/test_packaging.py` builds a real wheel and counts skills and
  migrations from the source tree. It needs no edit for this feature, but if
  it goes red, a file stopped being packaged - do not adjust the numbers.
- Nothing in this plan needs a migration. No schema changes at all.
- If a task's test passes the first time you run it, stop: you are testing
  behaviour that already existed, and the test is not proving what it claims.
