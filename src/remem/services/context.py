"""The knowledge base context block, harness-neutral.

Used to live inside the Claude Code hook (agents/claude_code/hook.py), which
meant a second harness could record events (services/record.py, already
harness-neutral) but had no way to be told anything - injection was Claude
Code only. This is the decision moved out to where the layering says it
belongs: a service takes a store and a project and returns a string, opening
no session and reading no payload, because those two things are exactly what
differs between harnesses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable
from uuid import UUID

from remem.services import kb
from remem.store import Store


def block(
    store: Store,
    owner_id: UUID,
    project: str,
    max_chars: int,
    note: Callable[[str], None] | None = None,
) -> str:
    """The knowledge base context block for one project, or "".

    Returns "" rather than raising for a project with no knowledge base:
    every caller is a fail-soft hook, and a missing knowledge base is an
    ordinary state, not an error.

    `note` is how the reason escapes. The debug messages this replaces went
    straight to REMEM_HOOK_DEBUG, which needs `env` - and a service that
    took `env` to decide where to print would be a service formatting
    output. So the service says what happened and the frontend decides
    where it goes: the Claude Code hook passes its `_debug`, the CLI passes
    its own, and a test passes a list's `append`.
    """
    say = note or (lambda _reason: None)

    rendered = ""
    try:
        collection = kb.get(store, owner_id, project)
    except kb.CollectionNotFound:
        say(
            f"no knowledge base with slug '{project}' for this principal. "
            "The hook injects the knowledge base whose slug matches the "
            f"repository name - create one with `remem kb new {project}`.",
        )
    else:
        entries = kb.resolve(store, owner_id, project)
        if entries:
            rendered = kb.render(collection, entries, max_chars)
        else:
            say(f"knowledge base '{project}' matched no entries")

    # Appended after render, outside max_chars on purpose: it is a fixed
    # ~20 tokens, and making it compete with rules for the budget would be
    # absurd. It is also emitted for a project with no knowledge base at
    # all, which is why the block is built rather than returned early.
    pointer = handoff_pointer(store, owner_id, project)
    return "\n".join(part for part in (rendered, pointer) if part)


def handoff_pointer(
    store: Store, owner_id: UUID, project: str, now: datetime | None = None
) -> str:
    """One line naming the live handoff, or "".

    Never raises: block()'s caller treats any exception as silence, but a
    helper that can throw turns a working knowledge base into no output at
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
