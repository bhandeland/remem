"""SessionStart hook.

Fail-soft is a hard requirement: bounded work, exit 0 unconditionally, print
nothing on error. A knowledge tool must never be why a session will not start.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Mapping

from remem.agents.claude_code.adapter import ClaudeCodeAdapter
from remem.config import load
from remem.services import kb
from remem.session import open_session


def _debug(env: Mapping[str, str], reason: str) -> None:
    """Explain an empty result, but only when REMEM_HOOK_DEBUG is set.

    Wrapped in its own try/except: an opt-in diagnostic must not be able to
    turn fail-soft into a failure. Nothing here ever writes to stdout - that
    stream is the context block and nothing else.
    """
    try:
        if env.get("REMEM_HOOK_DEBUG"):
            sys.stderr.write(f"remem hook: {reason}\n")
    except Exception:
        pass


def session_start(stdin_text: str, env: Mapping[str, str]) -> str:
    """Return a context block for the session's project, or "" for any problem."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except (json.JSONDecodeError, AttributeError):
        _debug(env, "stdin was not valid JSON")
        return ""

    try:
        identity = ClaudeCodeAdapter().identity(env, payload)
        if not identity.project:
            _debug(env, "the hook payload carried no cwd")
            return ""

        config = load(env=env)
        with open_session(config) as s:
            try:
                collection = kb.get(s.store, s.owner.id, identity.project)
            except kb.CollectionNotFound:
                _debug(
                    env,
                    f"no knowledge base with slug '{identity.project}' for "
                    f"principal '{s.owner.handle}'. The hook injects the "
                    "knowledge base whose slug matches the directory name - "
                    f"create one with `remem kb new {identity.project}`.",
                )
                return ""
            entries = kb.resolve(s.store, s.owner.id, identity.project)
            if not entries:
                _debug(
                    env,
                    f"knowledge base '{identity.project}' matched no entries",
                )
                return ""
            return kb.render(collection, entries, config.max_chars)
    except Exception as exc:
        # Any failure at all - unreachable database, missing migrations, an
        # over-budget knowledge base - is silence, never a broken session.
        _debug(env, f"{type(exc).__name__}: {exc}")
        return ""


def main() -> int:
    try:
        block = session_start(sys.stdin.read(), env=dict(os.environ))
        if block:
            sys.stdout.write(block)
    except Exception:
        pass
    return 0
