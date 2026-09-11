"""A failed delete must not skip `disable`.

The reserved project is forced into recording by round_trip() and must
never be left that way. The cleanup block ran both statements
unguarded, so an exception from the delete skipped the disable and left
__remem_verify__ recording for the rest of the process's life.
"""

from __future__ import annotations

from typing import Any

import pytest

import remem.services.record  # noqa: F401 - makes the submodule patchable by string path below
from remem.agents import verify


def test_a_failing_delete_still_disables_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled: list[str] = []

    class FakeStore:
        def delete_session_events(self, *args: Any) -> int:
            raise RuntimeError("delete blew up")

        def events_for_session(self, *args: Any) -> list[object]:
            return [object()]

    class FakeOwner:
        id = "owner-1"
        handle = "someone"

    class FakeSession:
        store = FakeStore()
        owner = FakeOwner()

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def fake_enable(store: object, owner: object, project: str) -> None:
        return None

    def recording_disable(store: object, owner: object, project: str) -> None:
        disabled.append(project)

    def fake_record_event(
        store: object, owner: object, event: object, agent: object
    ) -> object:
        return object()

    def fake_open_session(config: object) -> FakeSession:
        return FakeSession()

    fake_record = type(
        "FakeRecord",
        (),
        {
            "enable": staticmethod(fake_enable),
            "disable": staticmethod(recording_disable),
            "record": staticmethod(fake_record_event),
        },
    )

    monkeypatch.setattr("remem.config.load", lambda env=None: object())
    monkeypatch.setattr("remem.session.open_session", fake_open_session)
    monkeypatch.setattr("remem.services.record", fake_record)

    report = verify.round_trip("cursor", env={})

    assert disabled == [verify.VERIFY_PROJECT], (
        "disable must run even when the delete raises"
    )
    assert any("delete blew up" in w for w in report.warnings)


def test_a_failing_disable_is_reported_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two cleanup obligations are independent, so a failure in
    either one has to reach the report on its own."""
    deleted: list[str] = []

    class FakeStore:
        def delete_session_events(self, *args: Any) -> None:
            deleted.append(args[1])

        def events_for_session(self, *args: Any) -> list[object]:
            return [object()]

    class FakeOwner:
        id = "owner-1"
        handle = "someone"

    class FakeSession:
        store = FakeStore()
        owner = FakeOwner()

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def raising_disable(store: object, owner: object, project: str) -> None:
        raise RuntimeError("disable blew up")

    def fake_enable(store: object, owner: object, project: str) -> None:
        return None

    def fake_record_event(
        store: object, owner: object, event: object, agent: object
    ) -> object:
        return object()

    def fake_open_session(config: object) -> FakeSession:
        return FakeSession()

    fake_record = type(
        "FakeRecord",
        (),
        {
            "enable": staticmethod(fake_enable),
            "disable": staticmethod(raising_disable),
            "record": staticmethod(fake_record_event),
        },
    )

    monkeypatch.setattr("remem.config.load", lambda env=None: object())
    monkeypatch.setattr("remem.session.open_session", fake_open_session)
    monkeypatch.setattr("remem.services.record", fake_record)

    report = verify.round_trip("cursor", env={})

    assert deleted == [verify.VERIFY_PROJECT], (
        "the delete must still run even though disable is the one that fails"
    )
    assert any("disable blew up" in w for w in report.warnings)
