"""Every hook name on opencode's `Hooks` interface, vendored.

This file is a copy of an external fact, checked in on purpose. The
alternative - reading ~/.opencode/node_modules at test time - makes the
contract test skip wherever opencode is not installed, which is everywhere
that matters, CI included. A guard that skips is not a guard.

So the checked-in list is what the plugin is tested against, and a separate
`opencode`-marked test asserts this list still matches the installed types.
The two answer different questions; see tests/test_opencode_hooks_contract.py.

Transcribed from @opencode-ai/plugin 1.17.7,
dist/index.d.ts, `export interface Hooks`.
"""

from __future__ import annotations

import re
from pathlib import Path

PLUGIN_TYPES_VERSION = "1.17.7"

HOOK_NAMES = frozenset(
    {
        "dispose",
        "event",
        "config",
        "tool",
        "auth",
        "provider",
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
        "experimental.provider.small_model",
        "experimental.session.compacting",
        "experimental.compaction.autocontinue",
        "experimental.text.complete",
        "tool.definition",
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
    body = text[start : end if end != -1 else len(text)]
    return frozenset(_MEMBER.findall(body))
