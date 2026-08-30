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
    # across a 47MB file where an unrelated self-naming pair could sneak in.
    anchor = text.find('beforeSubmitPrompt:"beforeSubmitPrompt"')
    if anchor == -1:
        return frozenset()
    window = text[max(0, anchor - 2000) : anchor + 2000]
    return frozenset(_PAIR.findall(window))
