"""Judging what an adapter found. Every policy decision lives here.

The rule this file is built around: never report ok for something that
was not checked. That is the failure claude-mem's opencode integration
shipped for months, and the one the extract jobs reproduced with
entries_written=0.
"""

from __future__ import annotations

import re
from pathlib import Path

from remem.agents.base import ExpectedHook, HookState
from remem.services import doctor


def hooks(**found):
    expected = (
        ExpectedHook("SessionStart", "remem hook session-start", True, "injection"),
        ExpectedHook("PostToolUse", "remem hook record-event", True, "every tool call"),
        ExpectedHook("SessionEnd", "remem hook record-event", False, "prompt close"),
    )
    return HookState(
        expected=expected,
        path=Path("/tmp/settings.json"),
        found={h.event: tuple(found.get(h.event, ())) for h in expected},
    )


class FakeAdapter:
    name = "fake"

    def __init__(self, state=None, raises=False):
        self._state, self._raises = state, raises

    def hook_state(self, scope, home, env):
        if self._raises:
            raise RuntimeError("adapter is broken")
        return self._state


class NoCapability:
    name = "plain"


def verdicts(report):
    return {f.event: f.verdict for f in report.findings}


def test_a_registered_hook_is_ok():
    state = hooks(SessionStart=["remem hook session-start"],
                  PostToolUse=["remem hook record-event"],
                  SessionEnd=["remem hook record-event"])
    report = doctor.check({"fake": FakeAdapter(state)})[0]
    assert set(verdicts(report).values()) == {doctor.Verdict.OK}
    assert not doctor.failed([report])


def test_a_missing_required_hook_fails_the_check():
    state = hooks(SessionStart=["remem hook session-start"],
                  SessionEnd=["remem hook record-event"])
    report = doctor.check({"fake": FakeAdapter(state)})[0]
    assert verdicts(report)["PostToolUse"] is doctor.Verdict.MISSING
    assert doctor.failed([report])


def test_a_missing_optional_hook_is_reported_but_does_not_fail():
    state = hooks(SessionStart=["remem hook session-start"],
                  PostToolUse=["remem hook record-event"])
    report = doctor.check({"fake": FakeAdapter(state)})[0]
    assert verdicts(report)["SessionEnd"] is doctor.Verdict.MISSING
    assert not doctor.failed([report])


def test_a_hook_registered_twice_is_duplicated_and_does_not_fail():
    """It fires twice and doubles every row it records, which is worth
    saying loudly - but the hook does fire, so the exit code stays 0."""
    state = hooks(SessionStart=["remem hook session-start"],
                  PostToolUse=["remem hook record-event"] * 2)
    report = doctor.check({"fake": FakeAdapter(state)})[0]
    assert verdicts(report)["PostToolUse"] is doctor.Verdict.DUPLICATED
    assert not doctor.failed([report])


def test_a_superseded_command_is_stale_not_missing():
    state = hooks(SessionStart=["remem hook session-start"],
                  PostToolUse=["remem hook post-tool-use-old"])
    report = doctor.check({"fake": FakeAdapter(state)})[0]
    assert verdicts(report)["PostToolUse"] is doctor.Verdict.STALE
    assert not doctor.failed([report])


def test_an_adapter_with_no_remem_hooks_at_all_is_simply_not_installed():
    """Warning that Cursor's hooks are missing on a machine with no Cursor
    would make the whole report noise, and a report people skim hides the
    next PostToolUse."""
    report = doctor.check({"fake": FakeAdapter(hooks())})[0]
    assert report.installed is False
    assert report.findings == ()
    assert not doctor.failed([report])


def test_an_adapter_without_the_capability_is_unchecked_never_ok():
    report = doctor.check({"plain": NoCapability()})[0]
    assert report.verdict is doctor.Verdict.UNCHECKED
    assert report.findings == ()
    assert not doctor.failed([report])


def test_an_adapter_that_raises_warns_and_the_rest_still_run():
    """The registry contract: a broken third-party adapter must never be
    why remem will not run."""
    state = hooks(SessionStart=["remem hook session-start"],
                  PostToolUse=["remem hook record-event"],
                  SessionEnd=["remem hook record-event"])
    reports = doctor.check({
        "broken": FakeAdapter(raises=True),
        "fake": FakeAdapter(state),
    })
    broken = next(r for r in reports if r.agent == "broken")
    good = next(r for r in reports if r.agent == "fake")
    assert broken.verdict is doctor.Verdict.UNCHECKED
    assert "adapter is broken" in broken.warning
    assert set(verdicts(good).values()) == {doctor.Verdict.OK}


def test_unchecked_never_renders_as_ok():
    # A substring check for "ok" would also flag the word "hook" (h-ok),
    # which the very next assertion requires the text to contain - so this
    # checks for a standalone "ok" verdict marker, not any occurrence of
    # the letters.
    text = doctor.render(doctor.check({"plain": NoCapability()}))
    assert not re.search(r"\bok\b", text.lower())
    assert "no hook registration to check" in text.lower()


def test_the_advisory_names_the_hook_and_the_fix():
    state = hooks(SessionStart=["remem hook session-start"],
                  SessionEnd=["remem hook record-event"])
    lines = doctor.advisories(doctor.check({"fake": FakeAdapter(state)}))
    assert len(lines) == 1
    assert "PostToolUse" in lines[0]
    assert "remem doctor fake" in lines[0]


def test_a_complete_install_produces_no_advisory():
    state = hooks(SessionStart=["remem hook session-start"],
                  PostToolUse=["remem hook record-event"],
                  SessionEnd=["remem hook record-event"])
    assert doctor.advisories(doctor.check({"fake": FakeAdapter(state)})) == []


def test_json_and_human_forms_read_off_the_same_reports():
    state = hooks(SessionStart=["remem hook session-start"])
    reports = doctor.check({"fake": FakeAdapter(state)})
    data = doctor.to_dict(reports)
    assert data["failed"] is True
    assert data["agents"][0]["hooks"][1]["verdict"] == "missing"


class RaisesOnConstruction:
    """A third-party adapter whose __init__ blows up.

    Distinct from FakeAdapter(raises=True), which raises from hook_state -
    inside check()'s try. `registry.discover()` hands back classes, so the
    constructor call is real code on the ordinary path, and until this test
    existed nothing covered it.
    """

    name = "exploding"

    def __init__(self):
        raise RuntimeError("adapter blew up on construction")


def test_an_adapter_that_raises_from_init_warns_and_the_rest_still_run():
    state = hooks(SessionStart=["remem hook session-start"],
                  PostToolUse=["remem hook record-event"],
                  SessionEnd=["remem hook record-event"])
    reports = doctor.check({
        "exploding": RaisesOnConstruction,
        "fake": FakeAdapter(state),
    })
    broken = next(r for r in reports if r.agent == "exploding")
    good = next(r for r in reports if r.agent == "fake")
    assert broken.verdict is doctor.Verdict.UNCHECKED
    assert "blew up on construction" in broken.warning
    assert set(verdicts(good).values()) == {doctor.Verdict.OK}
