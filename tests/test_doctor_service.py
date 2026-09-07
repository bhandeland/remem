"""Judging what an adapter found. Every policy decision lives here.

The rule this file is built around: never report ok for something that
was not checked. That is the failure claude-mem's opencode integration
shipped for months, and the one the extract jobs reproduced with
entries_written=0.
"""

from __future__ import annotations

import re
from pathlib import Path

from remem.agents.base import ExpectedHook, HookState, UnsupportedScope
from remem.services import doctor


def hooks(path="/tmp/settings.json", **found):
    expected = (
        ExpectedHook("SessionStart", "remem hook session-start", True, "injection"),
        ExpectedHook("PostToolUse", "remem hook record-event", True, "every tool call"),
        ExpectedHook("SessionEnd", "remem hook record-event", False, "prompt close"),
    )
    return HookState(
        expected=expected,
        path=Path(path),
        found={h.event: tuple(found.get(h.event, ())) for h in expected},
    )


class FakeAdapter:
    """A single-scope adapter, which is what claude-code and opencode are.

    It raises UnsupportedScope for anything but "user" because that is what
    a real single-scope adapter does - and because doctor's sweep uses that
    raise as its skip signal. A fake that accepted every scope would answer
    twice for a harness that has only one place to look, and would let a
    sweep bug pass the suite.
    """

    name = "fake"

    def __init__(self, state=None, raises=False):
        self._state, self._raises = state, raises

    def hook_state(self, scope, home, env):
        if scope != "user":
            raise UnsupportedScope(f"scope '{scope}' is not supported")
        if self._raises:
            raise RuntimeError("adapter is broken")
        return self._state


class TwoScopeAdapter:
    """A both-scopes adapter, which is what cursor is. States by scope; a
    scope absent from the mapping is one the adapter does not support."""

    name = "two"

    def __init__(self, **states):
        self._states = states

    def hook_state(self, scope, home, env):
        if scope not in self._states:
            raise UnsupportedScope(f"scope '{scope}' is not supported")
        return self._states[scope]


class NoCapability:
    name = "plain"


def verdicts(report):
    return {f.event: f.verdict for f in report.findings}


def test_a_registered_hook_is_ok():
    state = hooks(
        SessionStart=["remem hook session-start"],
        PostToolUse=["remem hook record-event"],
        SessionEnd=["remem hook record-event"],
    )
    report = doctor.check({"fake": FakeAdapter(state)})[0]
    assert set(verdicts(report).values()) == {doctor.Verdict.OK}
    assert not doctor.failed([report])


def test_a_missing_required_hook_fails_the_check():
    state = hooks(
        SessionStart=["remem hook session-start"],
        SessionEnd=["remem hook record-event"],
    )
    report = doctor.check({"fake": FakeAdapter(state)})[0]
    assert verdicts(report)["PostToolUse"] is doctor.Verdict.MISSING
    assert doctor.failed([report])


def test_a_missing_optional_hook_is_reported_but_does_not_fail():
    state = hooks(
        SessionStart=["remem hook session-start"],
        PostToolUse=["remem hook record-event"],
    )
    report = doctor.check({"fake": FakeAdapter(state)})[0]
    assert verdicts(report)["SessionEnd"] is doctor.Verdict.MISSING
    assert not doctor.failed([report])


def test_a_hook_registered_twice_is_duplicated_and_does_not_fail():
    """It fires twice and doubles every row it records, which is worth
    saying loudly - but the hook does fire, so the exit code stays 0."""
    state = hooks(
        SessionStart=["remem hook session-start"],
        PostToolUse=["remem hook record-event"] * 2,
    )
    report = doctor.check({"fake": FakeAdapter(state)})[0]
    assert verdicts(report)["PostToolUse"] is doctor.Verdict.DUPLICATED
    assert not doctor.failed([report])


def test_a_superseded_command_is_stale_not_missing():
    state = hooks(
        SessionStart=["remem hook session-start"],
        PostToolUse=["remem hook post-tool-use-old"],
    )
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
    state = hooks(
        SessionStart=["remem hook session-start"],
        PostToolUse=["remem hook record-event"],
        SessionEnd=["remem hook record-event"],
    )
    reports = doctor.check(
        {
            "broken": FakeAdapter(raises=True),
            "fake": FakeAdapter(state),
        }
    )
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
    state = hooks(
        SessionStart=["remem hook session-start"],
        SessionEnd=["remem hook record-event"],
    )
    lines = doctor.advisories(doctor.check({"fake": FakeAdapter(state)}))
    assert len(lines) == 1
    assert "PostToolUse" in lines[0]
    assert "remem doctor fake" in lines[0]


def test_a_complete_install_produces_no_advisory():
    state = hooks(
        SessionStart=["remem hook session-start"],
        PostToolUse=["remem hook record-event"],
        SessionEnd=["remem hook record-event"],
    )
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
    state = hooks(
        SessionStart=["remem hook session-start"],
        PostToolUse=["remem hook record-event"],
        SessionEnd=["remem hook record-event"],
    )
    reports = doctor.check(
        {
            "exploding": RaisesOnConstruction,
            "fake": FakeAdapter(state),
        }
    )
    broken = next(r for r in reports if r.agent == "exploding")
    good = next(r for r in reports if r.agent == "fake")
    assert broken.verdict is doctor.Verdict.UNCHECKED
    assert "blew up on construction" in broken.warning
    assert set(verdicts(good).values()) == {doctor.Verdict.OK}


def complete(**over):
    base = dict(
        SessionStart=["remem hook session-start"],
        PostToolUse=["remem hook record-event"],
        SessionEnd=["remem hook record-event"],
    )
    base.update(over)
    return base


def test_a_sweep_reports_each_installed_scope_separately():
    """Two scopes are two files a harness will really read. Picking the
    first installed one hides a half-install in the other, which is the
    exact failure this whole command exists to catch."""
    adapter = TwoScopeAdapter(
        user=hooks("/home/u/.cursor/hooks.json", **complete()),
        project=hooks(
            "/repo/.cursor/hooks.json", SessionStart=["remem hook session-start"]
        ),
    )
    reports = doctor.check({"two": adapter})
    assert [(r.agent, r.scope) for r in reports] == [
        ("two", "user"),
        ("two", "project"),
    ]
    assert str(reports[0].path) == "/home/u/.cursor/hooks.json"
    assert verdicts(reports[1])["PostToolUse"] is doctor.Verdict.MISSING


def test_a_required_hook_missing_in_any_installed_scope_fails():
    """R4: the scopes are treated as independent. A healthy user scope does
    not excuse a broken project one - nothing here establishes that Cursor
    merges them, and the cost of staying quiet is unrecorded sessions."""
    adapter = TwoScopeAdapter(
        user=hooks("/home/u/.cursor/hooks.json", **complete()),
        project=hooks(
            "/repo/.cursor/hooks.json", SessionStart=["remem hook session-start"]
        ),
    )
    assert doctor.failed(doctor.check({"two": adapter}))


def test_an_uninstalled_adapter_is_reported_once_and_names_where_it_looked():
    adapter = TwoScopeAdapter(
        user=hooks("/home/u/.cursor/hooks.json"),
        project=hooks("/repo/.cursor/hooks.json"),
    )
    reports = doctor.check({"two": adapter})
    assert len(reports) == 1
    assert reports[0].installed is False
    text = doctor.render(reports)
    assert "/home/u/.cursor/hooks.json" in text
    assert "/repo/.cursor/hooks.json" in text


def test_a_single_scope_adapter_is_not_unchecked_by_the_sweep():
    """UnsupportedScope during a sweep is a skip, not a finding: the user
    asked "is this installed anywhere", and a scope the adapter does not
    support is not a place it could be."""
    reports = doctor.check({"fake": FakeAdapter(hooks(**complete()))})
    assert len(reports) == 1
    assert reports[0].scope == "user"
    assert reports[0].verdict is None


def test_an_explicit_scope_still_means_exactly_that_scope():
    """R1: naming a scope asks a specific question, and an adapter that
    cannot answer it must say so rather than render nothing. Sweeping here
    would print an empty report and exit 0 - this feature's cardinal sin."""
    reports = doctor.check({"fake": FakeAdapter(hooks(**complete()))}, scope="project")
    assert len(reports) == 1
    assert reports[0].verdict is doctor.Verdict.UNCHECKED
    assert "project" in reports[0].warning
    text = doctor.render(reports)
    assert "project" in text
    assert not re.search(r"\bok\b", text.lower())


def test_the_advisory_and_the_fix_line_both_carry_the_scope():
    """R5: a pointer that leads to a clean or contradictory screen teaches
    the user the advisory lies, and record status's credibility is the
    whole asset."""
    adapter = TwoScopeAdapter(
        project=hooks(
            "/repo/.cursor/hooks.json", SessionStart=["remem hook session-start"]
        ),
    )
    reports = doctor.check({"two": adapter})
    lines = doctor.advisories(reports)
    assert len(lines) == 1
    assert "project" in lines[0]
    assert "remem doctor two --scope project" in lines[0]
    assert "remem install two --scope project" in doctor.render(reports)


def test_a_stale_only_install_does_not_claim_to_be_incomplete():
    """The command IS registered, under a superseded name. Calling that
    "incomplete" sends the user looking for something that is not missing."""
    adapter = FakeAdapter(hooks(**complete(PostToolUse=["remem hook old"])))
    line = doctor.advisories(doctor.check({"fake": adapter}))[0]
    assert "incomplete" not in line
    assert "PostToolUse" in line


def test_render_with_no_adapters_says_so_rather_than_printing_nothing():
    """A blank page and exit 0 is a silent success for a question nobody
    answered - the same family as reporting ok for an unchecked adapter."""
    text = doctor.render([])
    assert text.strip()
    assert "no adapters" in text.lower()


def test_a_failed_check_reads_differently_from_nothing_to_check():
    """R7: a warning means something broke; its absence means the adapter
    simply has no hook configuration. The distinction is the honest one and
    needs no roster of which adapters remem ships."""
    broke = doctor.render(doctor.check({"fake": FakeAdapter(raises=True)}))
    nothing = doctor.render(doctor.check({"plain": NoCapability()}))
    assert broke != nothing
    assert "no hook registration to check" in nothing
    assert "no hook registration to check" not in broke


def test_json_rows_carry_the_scope():
    reports = doctor.check({"fake": FakeAdapter(hooks(**complete()))})
    assert doctor.to_dict(reports)["agents"][0]["scope"] == "user"
