"""Shared I/O helpers for fail-soft hook entry points.

Every hook - the Claude Code hooks in agents/claude_code/hook.py and the
`remem record event` CLI command, which is a hook entry point in everything
but name - shares the same diagnostic contract: stdout is reserved for the
hook's actual output, so an explanation of why nothing happened can only go
to stderr, and only when the caller opted in. Two copies of this helper
would drift; this is the one definition both sides call.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Mapping

from remem.extract.base import CHILD_ENV_VAR


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


def spawn_process(env: Mapping[str, str]) -> bool:
    """Start a detached `remem events process` and return immediately.

    Any session works off the backlog, so extraction is never stranded by the
    session that produced it having ended. Detached and output-discarded: the
    session must never wait for extraction, and must never see its output.

    This lives here rather than in the Claude Code adapter because nothing in
    it is about Claude Code, and every harness needs it. Claude Code triggers
    it from SessionStart; opencode and Cursor, which have no such hook, reach
    it through `remem hook context`. One trigger all three share beats a
    third install path bolted on for each new adapter.

    The CHILD_ENV_VAR guard is what keeps this from recursing: the extractor
    spawns `claude -p`, whose own hooks would otherwise spawn another
    extractor, which would read the events that run recorded, without bound.
    """
    if env.get(CHILD_ENV_VAR):
        return False
    try:
        subprocess.Popen(
            ["remem", "events", "process"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=dict(env),
        )
        return True
    except Exception:
        return False
