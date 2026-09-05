"""Claude Code hook entry points: SessionStart, PostToolUse/SessionEnd
recording, and the UserPromptSubmit session-size reminder.

Fail-soft is a hard requirement across all of them: bounded work, exit 0
unconditionally, print nothing on error. A knowledge tool must never be why a
session will not start, a tool call will not run, or a prompt will not send.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Mapping

from remem import session_size as session_size_mod
from remem.agents.claude_code.adapter import ClaudeCodeAdapter
from remem.config import load
from remem.extract.base import CHILD_ENV_VAR
from remem.hookio import debug as _debug
from remem.hookio import spawn_ingest, spawn_process
from remem.services import context, record
from remem.session import open_session


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
            return context.block(
                s.store,
                s.owner.id,
                identity.project,
                config.max_chars,
                note=lambda reason: _debug(env, reason),
                owner_handle=s.owner.handle,
            )
    except Exception as exc:
        # Any failure at all - unreachable database, missing migrations, an
        # over-budget knowledge base - is silence, never a broken session.
        _debug(env, f"{type(exc).__name__}: {exc}")
        return ""


def main() -> int:
    try:
        env = dict(os.environ)
        block = session_start(sys.stdin.read(), env=env)
        if block:
            sys.stdout.write(block)
        spawn_process(env)
        spawn_ingest(env)
    except Exception:
        pass
    return 0


def record_event(stdin_text: str, env: Mapping[str, str]) -> None:
    """Record one Claude Code hook payload as an event, or do nothing.

    This is the PostToolUse hook - it runs once per tool call - and, since
    `ClaudeCodeAdapter.event()` maps both payload shapes, it is also what
    `remem hook session-end` now points at: a `session_end` event shortens
    the idle wait extraction runs on, but the pipeline no longer needs it,
    which is what lets a harness without a SessionEnd hook lose nothing but
    time.

    Same fail-soft contract as every other hook: malformed stdin, an
    unreachable database, a project that has not opted in - all silent, all
    reported only under REMEM_HOOK_DEBUG, and none of them ever raise past
    here.
    """
    if env.get(CHILD_ENV_VAR):
        # The extractor's own `claude -p` child would otherwise record the
        # extraction itself as events, which the next extraction would then
        # read - an unbounded feedback loop. Both hooks check this; do not
        # add a third copy that checks something else.
        _debug(env, "inside an extraction child; not recording its events")
        return

    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except (json.JSONDecodeError, AttributeError):
        _debug(env, "stdin was not valid JSON")
        return

    try:
        adapter = ClaudeCodeAdapter()
        harness_event = adapter.event(env, payload)
        if harness_event is None:
            _debug(env, "payload was not an event worth recording")
            return

        config = load(env=env)
        with open_session(config) as s:
            result = record.record(
                s.store, s.owner.id, harness_event, adapter.name
            )
        if result is None:
            _debug(
                env,
                f"recording is not enabled for project "
                f"{harness_event.project!r}. Enable it with `remem record "
                f"enable --project {harness_event.project}`.",
            )
    except Exception as exc:
        _debug(env, f"{type(exc).__name__}: {exc}")


def main_record_event() -> int:
    try:
        record_event(sys.stdin.read(), env=dict(os.environ))
    except Exception:
        pass
    return 0


def session_size(stdin_text: str, env: Mapping[str, str]) -> str:
    """A reminder to hand off, or "" - which is most prompts.

    Same fail-soft contract as the other hooks, and one more reason for it:
    this runs on every single user prompt.
    """
    if env.get(CHILD_ENV_VAR):
        return ""

    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except (json.JSONDecodeError, AttributeError):
        _debug(env, "stdin was not valid JSON")
        return ""

    try:
        transcript_path = payload.get("transcript_path")
        if not transcript_path:
            _debug(env, "the hook payload carried no transcript_path")
            return ""
        session_id = payload.get("session_id") or "unknown"

        config = load(env=env)
        count = session_size_mod.count_turns(transcript_path)
        last = session_size_mod.read_last_warned(session_id)
        if not session_size_mod.should_warn(
            count, last, config.turn_warn_at, config.turn_warn_every
        ):
            return ""
        session_size_mod.record_warned(session_id, count)
        return session_size_mod.reminder(count)
    except Exception as exc:
        _debug(env, f"{type(exc).__name__}: {exc}")
        return ""


def main_session_size() -> int:
    try:
        text = session_size(sys.stdin.read(), env=dict(os.environ))
        if text:
            sys.stdout.write(text)
    except Exception:
        pass
    return 0
