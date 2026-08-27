"""Turning a session transcript into candidate entries.

The validator here does not trust the model. Everything a distiller returns is
untrusted text: it gets shape-checked, capped, and filtered before any of it
reaches the store.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from remem.domain import Kind

MAX_ENTRIES = 5
MAX_TITLE = 200
MAX_BODY = 4000

# Set on the environment of a spawned `claude -p` distillation child so its own
# hooks can detect they're running inside a capture and refuse to recurse.
# Defined here (not in the hook module) because base.py is layer-neutral: both
# the distiller and the agent hook can import it without depending on each
# other. This string MUST match wherever the child process checks it.
CHILD_ENV_VAR = "REMEM_CAPTURE_CHILD"

_ARRAY = re.compile(r"\[.*\]", re.DOTALL)


class DistillationFailed(Exception):
    """The distiller produced output that could not be read as entries."""


@dataclass(slots=True)
class CapturedEntry:
    title: str
    body: str
    kind: Kind
    tags: list[str] = field(default_factory=list)


class Distiller(Protocol):
    def distill(self, transcript: str, project: str) -> list[CapturedEntry]: ...


def _clean_tags(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [t.strip() for t in value if isinstance(t, str) and t.strip()]


def _entry_from(item: object) -> CapturedEntry | None:
    """One malformed entry drops itself rather than failing the whole batch."""
    if not isinstance(item, dict):
        return None
    title = item.get("title")
    body = item.get("body")
    if not isinstance(title, str) or not isinstance(body, str):
        return None
    if not title.strip() or not body.strip():
        return None
    try:
        kind = Kind(item.get("kind", "memory"))
    except ValueError:
        return None
    return CapturedEntry(
        title=title.strip()[:MAX_TITLE],
        body=body.strip()[:MAX_BODY],
        kind=kind,
        tags=_clean_tags(item.get("tags")),
    )


def parse_entries(raw: str) -> list[CapturedEntry]:
    """Read a distiller's raw output into validated entries.

    Accepts a bare JSON array, or one embedded in prose or a fenced block -
    models prepend explanations however firmly the prompt asks them not to.
    Raises DistillationFailed when no array can be found at all, or when the
    JSON is malformed, or when the top-level value isn't a list.

    An empty array ("[]") is a deliberate, successful "found nothing" result -
    most sessions contain nothing durable - and returns []. But a *non-empty*
    array where every item fails validation is different: the model produced
    output, and none of it was usable. That's a distillation failure, not a
    quiet clean session, so it raises rather than silently returning [] - it
    needs to show up in `remem capture status` instead of being indistinguishable
    from a week with nothing to capture.
    """
    match = _ARRAY.search(raw or "")
    if match is None:
        raise DistillationFailed("no JSON array in distiller output")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise DistillationFailed(f"output was not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise DistillationFailed("distiller output was not a list")

    if not data:
        return []

    entries = [e for e in (_entry_from(i) for i in data) if e is not None]
    if not entries:
        raise DistillationFailed("no entry in a non-empty array passed validation")
    return entries[:MAX_ENTRIES]
