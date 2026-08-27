"""SessionStart hook.

Fail-soft is a hard requirement: bounded work, exit 0 unconditionally, print
nothing on error. A knowledge tool must never be why a session will not start.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from typing import Mapping
from uuid import UUID

from remem import session_size as session_size_mod
from remem.agents.claude_code.adapter import ClaudeCodeAdapter
from remem.config import load
from remem.distill.base import CHILD_ENV_VAR
from remem.services import kb
from remem.session import open_session
from remem.store import Store


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
            block = ""
            try:
                collection = kb.get(s.store, s.owner.id, identity.project)
            except kb.CollectionNotFound:
                _debug(
                    env,
                    f"no knowledge base with slug '{identity.project}' for "
                    f"principal '{s.owner.handle}'. The hook injects the "
                    "knowledge base whose slug matches the repository name - "
                    f"create one with `remem kb new {identity.project}`.",
                )
            else:
                entries = kb.resolve(s.store, s.owner.id, identity.project)
                if entries:
                    block = kb.render(collection, entries, config.max_chars)
                else:
                    _debug(
                        env,
                        f"knowledge base '{identity.project}' matched no entries",
                    )

            # Appended after render, outside max_chars on purpose: it is a
            # fixed ~20 tokens, and making it compete with rules for the
            # budget would be absurd. It is also emitted for a project with no
            # knowledge base at all, which is why the block is built rather
            # than returned early.
            pointer = handoff_pointer(s.store, s.owner.id, identity.project)
            return "\n".join(part for part in (block, pointer) if part)
    except Exception as exc:
        # Any failure at all - unreachable database, missing migrations, an
        # over-budget knowledge base - is silence, never a broken session.
        _debug(env, f"{type(exc).__name__}: {exc}")
        return ""


def handoff_pointer(
    store: Store, owner_id: UUID, project: str, now: datetime | None = None
) -> str:
    """One line naming the live handoff, or "".

    Never raises: session_start's caller treats any exception as silence, but
    a helper that can throw turns a working knowledge base into no output at
    all, which is a worse failure than a missing pointer.
    """
    try:
        from remem.services import handoff

        entry = handoff.latest(store, owner_id, project=project)
        if entry is None or entry.created_at is None:
            return ""
        topic = handoff.topic_of(entry) or project
        age = handoff.age_phrase(
            entry.created_at, now or datetime.now(tz=entry.created_at.tzinfo)
        )
        return f"Handoff available: {topic} ({age}) - run remem-prime {topic}"
    except Exception:
        return ""


def spawn_drain(env: Mapping[str, str]) -> bool:
    """Start a detached `remem capture drain` and return immediately.

    Any session drains the backlog, so a capture is never stranded by the
    session that produced it having ended. Detached and output-discarded: the
    session must never wait for distillation, and must never see its output.
    """
    if env.get(CHILD_ENV_VAR):
        return False
    try:
        subprocess.Popen(
            ["remem", "capture", "drain"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=dict(env),
        )
        return True
    except Exception:
        return False


def main() -> int:
    try:
        env = dict(os.environ)
        block = session_start(sys.stdin.read(), env=env)
        if block:
            sys.stdout.write(block)
        spawn_drain(env)
    except Exception:
        pass
    return 0


def session_end(stdin_text: str, env: Mapping[str, str]) -> None:
    """Queue this session for distillation. Never raises, never prints.

    Does exactly one INSERT. Everything fragile - the subprocess, the model,
    the parsing - happens in the drain, where it can be retried and inspected.
    """
    if env.get(CHILD_ENV_VAR):
        # This session IS a distillation run. Enqueueing here would spawn
        # another distillation, without bound.
        _debug(env, "skipped: running inside a capture child")
        return

    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except (json.JSONDecodeError, AttributeError):
        _debug(env, "stdin was not valid JSON")
        return

    try:
        transcript_path = payload.get("transcript_path")
        if not transcript_path:
            _debug(env, "the hook payload carried no transcript_path")
            return

        identity = ClaudeCodeAdapter().identity(env, payload)
        if not identity.project:
            _debug(env, "the hook payload carried no cwd")
            return

        from remem.services import capture

        config = load(env=env)
        with open_session(config) as s:
            job = capture.enqueue(
                s.store,
                s.owner.id,
                project=identity.project,
                transcript_path=transcript_path,
                session_id=identity.session_id,
            )
            if job is None:
                _debug(
                    env,
                    f"capture is not enabled for project '{identity.project}'. "
                    f"Enable it with `remem capture enable --project "
                    f"{identity.project}`.",
                )
    except Exception:
        # Same contract as session_start: a knowledge tool must never be why a
        # session fails to close.
        _debug(env, "capture enqueue failed")


def main_session_end() -> int:
    try:
        session_end(sys.stdin.read(), env=dict(os.environ))
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
