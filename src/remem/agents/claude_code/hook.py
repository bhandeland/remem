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


def session_start(stdin_text: str, env: Mapping[str, str]) -> str:
    """Return a context block for the session's project, or "" for any problem."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except (json.JSONDecodeError, AttributeError):
        return ""

    try:
        identity = ClaudeCodeAdapter().identity(env, payload)
        if not identity.project:
            return ""

        config = load(env=env)
        with open_session(config) as s:
            try:
                collection = kb.get(s.store, s.owner.id, identity.project)
            except kb.CollectionNotFound:
                return ""
            entries = kb.resolve(s.store, s.owner.id, identity.project)
            if not entries:
                return ""
            return kb.render(collection, entries, config.max_chars)
    except Exception:
        # Any failure at all - unreachable database, missing migrations, an
        # over-budget knowledge base - is silence, never a broken session.
        return ""


def main() -> int:
    try:
        block = session_start(sys.stdin.read(), env=dict(os.environ))
        if block:
            sys.stdout.write(block)
    except Exception:
        pass
    return 0
