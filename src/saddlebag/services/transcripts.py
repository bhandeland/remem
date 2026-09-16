"""Transcript capture: every policy decision for storing raw session traces.

Frontends parse and format; they never decide. The rules that live here -
what a claim means, when a file is re-read, what counts as an anomaly, how
much a spawned refresh may do - are ones every frontend gets for free.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from saddlebag.store import Store

__all__ = [
    "Candidate",
    "discover",
]

#: The harness whose transcripts this reads. A constant rather than a
#: parameter because there is exactly one today and inventing the
#: generalisation before a second harness exists would be guessing at its
#: shape - the `harness` COLUMN is the part that costs nothing to have early.
HARNESS = "claude-code"


@dataclass
class Candidate:
    """A directory discovery believes belongs to a project, with its evidence."""

    path: str
    #: Transcripts in this directory whose session id has recorded events for
    #: this project. This is the proof; the name of the directory is not.
    matched: int
    #: Every transcript in the directory. Deliberately reported beside
    #: `matched`, because claiming imports all of them - including sessions
    #: from before recording existed, which can outnumber the matched ones
    #: several times over.
    total: int
    claimed_by: str | None


def discover(store: Store, owner_id: UUID, project: str, root: Path) -> list[Candidate]:
    """Propose directories that hold this project's sessions. Writes nothing.

    Ownership is PROVEN, not guessed: a transcript's filename is a session
    id, and `events` already records which project each session belongs to.
    Matching on the directory slug instead would have missed 184MB of this
    project's own history, because the tool was renamed partway through.

    The floor, stated rather than papered over: a directory whose sessions
    were never recorded cannot be found here at all. That is why this
    proposes and `designate` decides.
    """
    recorded = set(store.event_session_ids(owner_id, project))
    if not recorded or not root.is_dir():
        return []

    claims = {p.path: p.project for p in store.transcript_paths(owner_id)}

    found: list[Candidate] = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        files = sorted(directory.glob("*.jsonl"))
        if not files:
            continue
        matched = sum(1 for f in files if f.stem in recorded)
        if matched == 0:
            continue
        found.append(
            Candidate(
                path=str(directory),
                matched=matched,
                total=len(files),
                claimed_by=claims.get(str(directory)),
            )
        )
    return found
