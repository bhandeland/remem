"""A failed delete must not skip `disable`.

The reserved project is forced into recording by round_trip() and must
never be left that way. The cleanup block ran both statements
unguarded, so an exception from the delete skipped the disable and left
__remem_verify__ recording for the rest of the process's life.
"""

from __future__ import annotations

import remem.services.record  # noqa: F401 - makes the submodule patchable by string path below
from remem.agents import verify


def test_a_failing_delete_still_disables_recording(monkeypatch):
    disabled: list[str] = []

    class FakeStore:
        def delete_session_events(self, *args):
            raise RuntimeError("delete blew up")

        def events_for_session(self, *args):
            return [object()]

    class FakeOwner:
        id = "owner-1"
        handle = "someone"

    class FakeSession:
        store = FakeStore()
        owner = FakeOwner()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake_record = type(
        "FakeRecord",
        (),
        {
            "enable": staticmethod(lambda store, owner, project: None),
            "disable": staticmethod(
                lambda store, owner, project: disabled.append(project)
            ),
            "record": staticmethod(lambda store, owner, event, agent: object()),
        },
    )

    monkeypatch.setattr("remem.config.load", lambda env=None: object())
    monkeypatch.setattr("remem.session.open_session", lambda config: FakeSession())
    monkeypatch.setattr("remem.services.record", fake_record)

    report = verify.round_trip("cursor", env={})

    assert disabled == [verify.VERIFY_PROJECT], (
        "disable must run even when the delete raises"
    )
    assert any("delete blew up" in w for w in report.warnings)


def test_a_failing_disable_is_reported_too(monkeypatch):
    """The two cleanup obligations are independent, so a failure in
    either one has to reach the report on its own."""
    deleted: list[str] = []

    class FakeStore:
        def delete_session_events(self, *args):
            deleted.append(args[1])

        def events_for_session(self, *args):
            return [object()]

    class FakeOwner:
        id = "owner-1"
        handle = "someone"

    class FakeSession:
        store = FakeStore()
        owner = FakeOwner()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def raising_disable(store, owner, project):
        raise RuntimeError("disable blew up")

    fake_record = type(
        "FakeRecord",
        (),
        {
            "enable": staticmethod(lambda store, owner, project: None),
            "disable": staticmethod(raising_disable),
            "record": staticmethod(lambda store, owner, event, agent: object()),
        },
    )

    monkeypatch.setattr("remem.config.load", lambda env=None: object())
    monkeypatch.setattr("remem.session.open_session", lambda config: FakeSession())
    monkeypatch.setattr("remem.services.record", fake_record)

    report = verify.round_trip("cursor", env={})

    assert deleted == [verify.VERIFY_PROJECT], (
        "the delete must still run even though disable is the one that fails"
    )
    assert any("disable blew up" in w for w in report.warnings)
