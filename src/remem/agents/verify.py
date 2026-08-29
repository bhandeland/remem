"""The install round-trip, shared by every adapter.

Record an event, read it back, delete it. An install that reports success
without demonstrating anything is how claude-mem's opencode integration
recorded nothing for months. The round-trip is deliberately end-to-end - it
goes through the same `remem record event` the hook will call, not through a
store handle the hook does not have - because what is being tested is the
wiring, and every part of the wiring that this skips is a part that can be
broken while the check passes.

Harness-independent by design: the only thing that varies between adapters
is the name recorded in the `harness` column, passed in as `agent_name`.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Mapping

from remem.agents.base import HarnessEvent, InstallReport
from remem.domain import EventKind, new_id

#: The reserved project install verification round-trips through. Nothing
#: else ever writes to it, which is what lets round_trip() force recording
#: on, write, read back, and delete without touching a project the user
#: chose.
VERIFY_PROJECT = "__remem_verify__"


def round_trip(
    agent_name: str, env: Mapping[str, str] | None = None
) -> InstallReport:
    """Record an event under `agent_name`, read it back, delete it.

    Never raises - a failure here is a warning naming what could not be
    proven, and the caller (a user typing `remem verify`, or `install()` on
    its way out) still finishes.
    """
    from remem.config import load
    from remem.services import record as record_service
    from remem.session import open_session

    report = InstallReport(agent=agent_name)
    env = dict(os.environ) if env is None else dict(env)
    session_id = str(new_id())
    try:
        config = load(env=env)
        with open_session(config) as s:
            record_service.enable(s.store, s.owner.id, VERIFY_PROJECT)
            try:
                harness_event = HarnessEvent(
                    kind=EventKind.TOOL_CALL,
                    session_id=session_id,
                    project=VERIFY_PROJECT,
                    tool="remem-verify",
                    payload={"verify": True},
                    occurred_at=datetime.now(timezone.utc),
                )
                stored = record_service.record(
                    s.store, s.owner.id, harness_event, agent_name
                )
                if stored is None:
                    report.warnings.append(
                        "install verification could not record a test "
                        "event - recording did not stay enabled for "
                        f"'{VERIFY_PROJECT}'"
                    )
                    return report

                readback = s.store.events_for_session(
                    s.owner.id, VERIFY_PROJECT, agent_name, session_id
                )
                if not readback:
                    report.warnings.append(
                        "install verification recorded a test event "
                        "but could not read it back"
                    )
                    return report

                report.actions.append(
                    "Verified the install with a live round-trip: "
                    "recorded, read back, and deleted a test event"
                )
            finally:
                # Runs on every path out of the block above - the happy
                # path, the "could not record" return, the "could not read
                # it back" return, and any exception - because the reserved
                # project must never be left recording, and the test event
                # it wrote must never be left behind, however the
                # round-trip went.
                #
                # Scoped to exactly this (owner, project, harness,
                # session) - never `services.events.prune`, whose
                # contract is a time window over every event this owner
                # has ever recorded, in every project, and which a
                # `force=True` call here would have deleted wholesale.
                #
                # The two statements are guarded separately, and that is
                # the point: they are independent obligations, and a
                # failure to delete one scratch row in a project nothing
                # reads must not be why recording is left enabled. The
                # disable is the more important of the two.
                try:
                    s.store.delete_session_events(
                        s.owner.id, VERIFY_PROJECT, agent_name, session_id
                    )
                except Exception as exc:
                    report.warnings.append(
                        "install verification could not delete its test "
                        f"event: {type(exc).__name__}: {exc}"
                    )
                try:
                    record_service.disable(s.store, s.owner.id, VERIFY_PROJECT)
                except Exception as exc:
                    report.warnings.append(
                        "install verification could not disable recording "
                        f"for '{VERIFY_PROJECT}': {type(exc).__name__}: {exc}"
                    )
    except Exception as exc:
        report.warnings.append(
            f"could not verify the install: {type(exc).__name__}: {exc}"
        )
    return report
