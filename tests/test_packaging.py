"""The built wheel must contain the non-Python files remem needs at runtime.

Nothing else in the suite can catch a packaging regression. The install tests
reach the migrations and skills through `resources.files()`, which under the
mandatory editable install resolves straight back to the source tree - so a
wheel that shipped none of them would leave every one of those tests green and
break only on a real, non-editable install.

The expected contents are counted from the source tree rather than hardcoded.
A literal "5 migrations, 4 skills" would fail the day a sixth migration lands,
get edited to say 6, and from then on assert only that someone updated the
number. Deriving both sides means the test keeps asking the question it was
written to ask: does the wheel match the source?
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "remem"
MIGRATIONS = SRC / "backends" / "postgres" / "migrations"
SKILLS = SRC / "agents" / "claude_code" / "skills"
OPENCODE_PLUGIN = SRC / "agents" / "opencode" / "plugin.js"


@pytest.fixture(scope="module")
def wheel_names(tmp_path_factory) -> set[str]:
    """Build a wheel once and return the archive paths it contains."""
    out = tmp_path_factory.mktemp("wheel")
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out)],
        cwd=SRC.parent.parent,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"uv build failed:\n{result.stdout}\n{result.stderr}")
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    with zipfile.ZipFile(wheels[0]) as zf:
        return set(zf.namelist())


@pytest.mark.slow
def test_the_wheel_ships_every_migration(wheel_names):
    expected = {
        f"remem/backends/postgres/migrations/{p.name}" for p in MIGRATIONS.glob("*.sql")
    }
    assert expected, "no migrations found in the source tree - test is broken"
    assert expected <= wheel_names


@pytest.mark.slow
def test_the_wheel_ships_every_skill(wheel_names):
    expected = {
        # Skills are a directory of arbitrary supporting files, not just
        # SKILL.md, so compare every file rather than just the manifests.
        f"remem/agents/claude_code/skills/{p.relative_to(SKILLS).as_posix()}"
        for p in SKILLS.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    assert expected, "no skills found in the source tree - test is broken"
    assert expected <= wheel_names


@pytest.mark.slow
def test_the_wheel_ships_the_opencode_plugin(wheel_names):
    # plugin.js is read at install time via `resources.files()`
    # (agents/opencode/adapter.py), the exact same trap the install tests
    # fall into for migrations and skills under an editable install: it
    # resolves straight back to the source tree and stays green even if the
    # wheel shipped none of it.
    assert OPENCODE_PLUGIN.is_file(), "plugin.js missing from the source tree"
    assert "remem/agents/opencode/plugin.js" in wheel_names


@pytest.mark.slow
def test_the_wheel_ships_the_console_script(wheel_names):
    # The MCP server and both hooks are registered as a bare `remem`; without
    # the entry point the generated Claude Code config silently does nothing.
    assert any(n.endswith(".dist-info/entry_points.txt") for n in wheel_names)
