"""Recording: the per-project opt-in, and the single INSERT behind it.

The gate is the entire safety story of the events pipeline. Full payloads
mean this table will hold file contents, command output, and whatever a user
pasted into a prompt - so what decides whether any of that is ever written is
one boolean per project, checked here, in the service, where every frontend
gets it and no frontend can skip it.
"""

from __future__ import annotations

from uuid import UUID

from remem.agents.base import HarnessEvent
from remem.domain import Event, new_id
from remem.store import Store


class RecordingDisabled(Exception):
    """Recording is not enabled for this project.

    Not raised on the hook path - a hook treats a closed gate as an
    ordinary, silent outcome. `remem record event` raises it only to explain
    itself under `--json` or `--strict`, where a human is asking why nothing
    happened.
    """

    def __init__(self, project: str | None):
        super().__init__(f"recording is not enabled for project {project!r}")
        self.project = project


def enable(store: Store, owner_id: UUID, project: str) -> None:
    store.set_record_enabled(owner_id, project, True)


def disable(store: Store, owner_id: UUID, project: str) -> None:
    store.set_record_enabled(owner_id, project, False)


def is_enabled(store: Store, owner_id: UUID, project: str) -> bool:
    return store.record_enabled(owner_id, project)


def record(
    store: Store, owner_id: UUID, harness_event: HarnessEvent, harness: str
) -> Event | None:
    """Store one event, or return None if it may not be recorded.

    Two ways to fail closed, both silent: no project (the gate has nothing
    to check against) and a project that has not opted in. Neither raises -
    that is the caller's call to make, not this function's.
    """
    if harness_event.project is None:
        return None
    if not store.record_enabled(owner_id, harness_event.project):
        return None
    return store.put_event(
        Event(
            id=new_id(),
            owner_id=owner_id,
            project=harness_event.project,
            harness=harness,
            session_id=harness_event.session_id,
            kind=harness_event.kind,
            payload=harness_event.payload,
            tool=harness_event.tool,
            occurred_at=harness_event.occurred_at,
        )
    )
