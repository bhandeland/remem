"""Shared I/O helpers for fail-soft hook entry points.

Every hook - the Claude Code hooks in agents/claude_code/hook.py and the
`remem record event` CLI command, which is a hook entry point in everything
but name - shares the same diagnostic contract: stdout is reserved for the
hook's actual output, so an explanation of why nothing happened can only go
to stderr, and only when the caller opted in. Two copies of this helper
would drift; this is the one definition both sides call.
"""

from __future__ import annotations

import sys
from typing import Mapping


def debug(env: Mapping[str, str], reason: str) -> None:
    """Explain an empty result, but only when REMEM_HOOK_DEBUG is set.

    Wrapped in its own try/except: an opt-in diagnostic must not be able to
    turn fail-soft into a failure. Nothing here ever writes to stdout - that
    stream is reserved for the hook's real output.
    """
    try:
        if env.get("REMEM_HOOK_DEBUG"):
            sys.stderr.write(f"remem hook: {reason}\n")
    except Exception:
        pass
