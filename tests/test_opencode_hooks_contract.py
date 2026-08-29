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
