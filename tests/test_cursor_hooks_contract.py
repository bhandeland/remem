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
    """The reader, exercised without needing Cursor installed.

    The fixture has to exercise both halves of the real reader: the anchor
    (`beforeSubmitPrompt:"beforeSubmitPrompt"`, which the enumeration must
    contain for the reader to find the object at all) and the +/-2000
    character window around it (`notAHook` sits far enough past the
    enumeration - separated by 3000 characters of padding - that it falls
    outside the window, the same way an unrelated self-naming pair
    elsewhere in the real 47MB bundle would).
    """
    bundle = tmp_path / "Contents" / "Resources" / "app" / "out" / "vs" / "workbench"
    bundle.mkdir(parents=True)
    enumeration = (
        'x={beforeShellExecution:"beforeShellExecution",'
        'postToolUse:"postToolUse",sessionStart:"sessionStart",'
        'workspaceOpen:"workspaceOpen",'
        'beforeSubmitPrompt:"beforeSubmitPrompt"}}}),'
    )
    padding = "x" * 3000
    tail = 'other={notAHook:"notAHook"}'
    (bundle / "workbench.desktop.main.js").write_text(enumeration + padding + tail)

    names = installed_hook_names(tmp_path)

    assert names == {
        "beforeShellExecution",
        "postToolUse",
        "sessionStart",
        "workspaceOpen",
        "beforeSubmitPrompt",
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
