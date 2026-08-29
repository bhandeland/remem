"""Pruning raw events - the one place in this pipeline that deletes anything.

The policy lives here, not in the store: what counts as a valid window, and
whether an unextracted event may be deleted at all. The store just executes
the delete this module has already decided is safe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from remem.store import Store

_WINDOW_RE = re.compile(r"^([1-9][0-9]*)([dhm])$")

_UNITS = {"d": "days", "h": "hours", "m": "minutes"}


class BadWindow(Exception):
    """`--before` did not parse. No default unit is guessed for a bare
    number - a user who meant 30 days and typed "30" must not silently
    delete 30 minutes' worth, or everything."""


class PruneRefused(Exception):
    """Refused: the window matched nothing but unextracted events.

    A run that deletes some events and skips others is a normal, successful
    prune - see `prune`. This is raised only for the run that would
    otherwise silently do nothing at all: every event it found in the
    window is raw that has not produced anything yet. Carries the count so
    the caller can report exactly what stood in the way, and so `--force`
    has something concrete to override.
    """

    def __init__(self, unextracted: int):
        super().__init__(
            f"{unextracted} event(s) in this window have not been extracted "
            "yet; pass --force to delete them anyway"
        )
        self.unextracted = unextracted


@dataclass(slots=True)
class PruneReport:
    deleted: int
    dangling: int
    kept_unextracted: int


def parse_window(text: str) -> timedelta:
    """Parse "30d", "12h", "90m" into a timedelta. Nothing else.

    No bare numbers (which unit?), no negatives, no zero (a window that
    deletes everything or nothing is never what "--before" means), and no
    whitespace variants - one canonical form, so a typo fails loudly instead
    of being interpreted as something else.
    """
    match = _WINDOW_RE.match(text)
    if not match:
        raise BadWindow(f"'{text}' is not a window like 30d, 12h, or 90m")
    amount = int(match.group(1))
    unit = _UNITS[match.group(2)]
    return timedelta(**{unit: amount})


def prune(
    store: Store, owner_id: UUID, *, before: datetime, force: bool = False
) -> PruneReport:
    """Delete events older than `before` that have already been extracted.

    Per the design's retention section: prune deletes events that are both
    older than the window and already extracted, and unextracted events are
    never deleted by default - they are silently skipped, and the run still
    succeeds. A run that skips some events while deleting others is not a
    refusal; it is reported normally, `kept_unextracted` included, so the
    user sees exactly what was left behind and why.

    The refusal is reserved for the case that would otherwise look like
    nothing happened for no visible reason: the window matched only
    unextracted events, so `deleted` came back 0 while `kept_unextracted`
    did not. `--force` (for a stuck session whose extraction will never
    finish) removes the extraction requirement entirely, so nothing is ever
    kept back and nothing is ever refused.
    """
    deleted, dangling, kept_unextracted = store.prune_events(
        owner_id, before=before, force=force
    )
    if not force and deleted == 0 and kept_unextracted > 0:
        raise PruneRefused(kept_unextracted)
    return PruneReport(
        deleted=deleted, dangling=dangling, kept_unextracted=kept_unextracted
    )
