"""Is what is on disk what this adapter installs?

The failure this answers: on 2026-08-30 Claude Code had recorded zero tool
calls for the life of the events pipeline, because settings.json held three
hooks and not four. Hooks are fail-soft and print nothing, `record status`
reports what was recorded and cannot know what should have been, and extract
jobs finished `done` with entries_written=0 - indistinguishable from a quiet
session. Nothing in remem could see it.

Adapters answer with facts (`HookState`); every judgement is made here, so
that all of them agree on what "missing" means and so one computation feeds
`remem doctor`, its --json form, and the advisory line in
`remem record status`.

Opens no database connection. `remem doctor` must work with Postgres down,
for the same reason `services/settings.py` does: a diagnostic that needs the
system to be healthy is no use when it is not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping

from remem.agents.base import HookState, UnsupportedScope


#: The scopes a sweep examines. remem's, not the adapters' - the vocabulary
#: is already closed and already remem-wide, since `--scope` accepts exactly
#: these two words everywhere and every adapter hardcodes its accept-list
#: from them. A probed `scopes` capability would be a new seam for every
#: third-party adapter author to learn about, to answer a question remem
#: already knows the answer to. `UnsupportedScope` is the skip signal.
SCOPES: tuple[str, ...] = ("user", "project")


class Verdict(StrEnum):
    OK = "ok"
    MISSING = "missing"
    #: Registered more than once. It fires twice and doubles every row it
    #: records - what 525b491 detects downstream by inferring from duplicate
    #: event rows, visible here before a single duplicate row is written.
    DUPLICATED = "duplicated"
    #: A superseded command remains where the canonical one belongs. The
    #: hook still fires, because remem's legacy commands are real aliases,
    #: so this is a warning and not a failure.
    STALE = "stale"
    #: Not examined - the adapter has no hook configuration to check, or its
    #: implementation raised. Never rendered as `ok`; reporting success for
    #: something never verified is the exact failure this module exists to
    #: prevent.
    UNCHECKED = "unchecked"


@dataclass(frozen=True, slots=True)
class Finding:
    event: str
    verdict: Verdict
    required: bool
    provides: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AgentReport:
    agent: str
    #: The scope this report is about, or None for an adapter that is
    #: installed in no scope at all. One report per (adapter, scope) that is
    #: installed: two scopes are two files a real harness will really read,
    #: and any scheme that picked "the first installed scope" would hide the
    #: half-install this module exists to catch.
    scope: str | None = None
    #: The config file examined, or None - either because the adapter has no
    #: hook configuration or because the file does not exist.
    path: Path | None = None
    #: An adapter is INSTALLED when at least one remem command appears in
    #: its config. Zero is "not installed" and stays quiet; some-but-not-all
    #: is the bug this module exists to catch and is never quiet.
    installed: bool = False
    findings: tuple[Finding, ...] = ()
    #: Every path looked at while concluding "not installed". Set only on a
    #: not-installed report, and rendered: an adapter reported absent must
    #: say where it looked, or the user cannot tell a genuine absence from a
    #: sweep that never examined the place it is actually installed.
    examined: tuple[Path, ...] = ()
    #: Set only when the whole adapter could not be examined.
    verdict: Verdict | None = None
    warning: str | None = None


def _judge(state: HookState) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for hook in state.expected:
        commands = tuple(state.found.get(hook.event, ()))
        if not commands:
            verdict, detail = Verdict.MISSING, ""
        elif len(commands) > 1:
            verdict = Verdict.DUPLICATED
            detail = f"registered {len(commands)} times"
        elif commands[0] != hook.command:
            verdict = Verdict.STALE
            detail = f"names `{commands[0]}`, superseded by `{hook.command}`"
        else:
            verdict, detail = Verdict.OK, ""
        findings.append(Finding(
            event=hook.event, verdict=verdict,
            required=hook.required, provides=hook.provides, detail=detail,
        ))
    return tuple(findings)


def check(
    adapters: Mapping[str, object],
    scope: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[AgentReport]:
    """Reports in adapter-name order, one per (adapter, scope) installed.

    `scope=None` - the default, and what both frontends pass unless the user
    typed `--scope` - sweeps `SCOPES`. A named scope examines exactly that
    scope. The two are different questions and get different answers to
    UnsupportedScope:

    - Sweeping, the user asked "is this installed anywhere". A scope the
      adapter does not support is not a place it could be, so the raise is a
      SKIP. This is what stops `remem doctor` reporting cursor "not
      installed" while cursor is installed at project scope, which is its
      ordinary real-world shape.
    - Named, the user asked a specific question. UnsupportedScope stays
      UNCHECKED-with-a-warning, because "I could not tell" is the honest
      reply. Skipping instead would make `remem doctor claude-code --scope
      project` print nothing and exit 0 - a silent success for a question
      nobody answered, which is the failure this whole module exists to end.

    `hook_state` is probed with getattr rather than required, exactly as
    `event()`, `env_settings()`, `settings_path()` and `inject()` are: a
    third-party adapter written before this existed must keep working, and
    a broken one must never be why `remem doctor` will not run.
    """
    home = home or Path.home()
    env = dict(os.environ) if env is None else dict(env)
    reports: list[AgentReport] = []

    for name in sorted(adapters):
        # Constructing the adapter and probing the capability are inside the
        # guard, not outside it: `registry.discover()` hands back classes, so
        # `adapter()` is real third-party code running on the ordinary path,
        # and an adapter with a `__getattr__` can raise from the probe too.
        # Either one escaping would take `remem doctor` down with a traceback,
        # which is the exact contract this module's docstring claims to keep.
        try:
            adapter = adapters[name]
            adapter = adapter() if isinstance(adapter, type) else adapter
            state_fn = getattr(adapter, "hook_state", None)
        except Exception as exc:
            reports.append(AgentReport(
                agent=name, verdict=Verdict.UNCHECKED,
                warning=f"could not load {name}'s adapter: {exc}",
            ))
            continue
        if state_fn is None:
            reports.append(AgentReport(agent=name, verdict=Verdict.UNCHECKED))
            continue
        sweeping = scope is None
        scopes = SCOPES if sweeping else (scope,)
        examined: list[Path] = []
        answered = 0
        for one in scopes:
            try:
                state = state_fn(one, home, env)
            except UnsupportedScope as exc:
                # Skip while sweeping, report while asked - see the docstring.
                if sweeping:
                    continue
                answered += 1
                reports.append(AgentReport(
                    agent=name, scope=one, verdict=Verdict.UNCHECKED,
                    warning=str(exc),
                ))
                continue
            except Exception as exc:
                # One scope failing does not make the others unknowable, so
                # this is per-scope rather than per-adapter: a report for the
                # scope that broke, and real answers for the ones that did not.
                answered += 1
                reports.append(AgentReport(
                    agent=name, scope=one, verdict=Verdict.UNCHECKED,
                    warning=f"could not read {name}'s hook configuration: {exc}",
                ))
                continue

            if state.path is not None:
                examined.append(state.path)
            if any(state.found.get(h.event) for h in state.expected):
                answered += 1
                reports.append(AgentReport(
                    agent=name,
                    scope=one,
                    path=state.path,
                    installed=True,
                    findings=_judge(state),
                ))

        # "Not installed" is said once per adapter and only when no scope
        # held a remem command, never per scope: an adapter absent from both
        # files is one fact, not two. It is withheld entirely when some scope
        # could not be read, because an unread file is not evidence of
        # absence - that adapter already has its UNCHECKED row.
        if answered == 0:
            reports.append(AgentReport(
                agent=name, installed=False, examined=tuple(examined),
            ))
    return reports


def failed(reports: list[AgentReport]) -> bool:
    """True when a required hook is missing on any adapter.

    Only MISSING-and-required fails. STALE and DUPLICATED both still fire
    the hook, and UNCHECKED reports the absence of a check rather than the
    presence of a fault - exiting non-zero for "I could not tell" would
    train users to ignore the exit code, and the exit code is the half a
    script reads.

    A required hook missing in ANY installed scope fails, even when another
    scope is complete. That is a decision, not an oversight. Nothing in this
    repository establishes whether Cursor unions its user and project hooks
    or lets one override the other, so doctor cannot derive the answer and
    has to pick one on the cost of being wrong. If the scopes do merge, this
    raises a false alarm naming the exact file it read, which a user
    dismisses in seconds by running one idempotent `remem install`. The
    other direction stays silent about a real break because the *other*
    scope looked healthy, and unrecorded sessions are the failure that
    produced this feature. The noise gate makes the trade cheap: a scope
    with zero remem commands is "not installed" and produces no findings at
    all, so this only ever fires on a genuinely half-populated install.
    """
    return any(
        f.verdict is Verdict.MISSING and f.required
        for r in reports for f in r.findings
    )


def advisories(reports: list[AgentReport]) -> list[str]:
    """One line per installed-and-imperfect (adapter, scope), for `record
    status` - which is what a user runs when a harness looks quiet, and is
    otherwise structurally incapable of answering.

    Every line names its scope and points at the scoped command, because a
    pointer must reproduce its own finding. If the advisory said `remem
    doctor cursor` and that command - sweeping, and reporting both scopes -
    showed a clean or contradictory picture, the user would learn the
    advisory line lies. `record status` is the one place a harness that
    recorded nothing ever becomes visible, and its credibility is the whole
    asset. An adapter broken in two scopes produces two lines: two files,
    two fixes.
    """
    lines: list[str] = []
    for report in reports:
        broken = [f for f in report.findings if f.verdict is not Verdict.OK]
        if not broken:
            continue
        named = ", ".join(f"{f.event} ({f.verdict})" for f in broken)
        lines.append(
            f"{report.agent} is installed at {report.scope} scope but "
            f"{_complaint(broken)}: {named} - run "
            f"`remem doctor {report.agent} --scope {report.scope}`"
        )
    return lines


def _complaint(broken: list[Finding]) -> str:
    """"Incomplete" is only true when something is actually absent.

    A STALE-only install has every hook registered, under a superseded
    command name that still runs - calling that incomplete sends the user
    looking for a hook that is not missing.
    """
    absent = any(f.verdict is Verdict.MISSING for f in broken)
    return "its hooks are incomplete" if absent else "its hooks need attention"


def render(reports: list[AgentReport]) -> str:
    if not reports:
        # Blank output and exit 0 reads as "everything is fine" for a
        # question nobody answered - the same silent success as reporting ok
        # for an adapter that was never examined.
        return "no adapters found - nothing to check"
    lines: list[str] = []
    for report in reports:
        if report.verdict is Verdict.UNCHECKED:
            # A warning present means something failed; its absence means the
            # adapter simply has no hook configuration to check (opencode
            # ships a plugin file instead). Rendering the two the same way
            # buries a real breakage - possibly a bug in an adapter remem
            # itself ships - in a line that reads like an ordinary absence.
            # This distinction needs no roster of first-party adapters,
            # which is why it is drawn here and not from a list.
            if report.warning:
                where = f" at {report.scope} scope" if report.scope else ""
                lines.append(f"{report.agent}{where}: COULD NOT CHECK")
                lines.append(f"  {report.warning}")
            else:
                lines.append(f"{report.agent}: no hook registration to check")
            continue
        if not report.installed:
            lines.append(f"{report.agent}: not installed")
            # Where it looked, always. "Not installed" asserted without a
            # path is indistinguishable from a sweep that never examined the
            # file the adapter is actually installed in.
            for path in report.examined:
                lines.append(f"  looked in {path}")
            if not report.examined:
                lines.append("  no configuration path to examine")
            continue
        lines.append(f"{report.agent} ({report.scope})  {report.path}")
        for f in report.findings:
            mark = "ok" if f.verdict is Verdict.OK else f.verdict.upper()
            note = f"  - {f.detail or f.provides}" if f.verdict is not Verdict.OK else ""
            lines.append(f"  {f.event:<18} {mark}{note}")
        if any(f.verdict is not Verdict.OK for f in report.findings):
            # Scoped, for the same reason the advisory line is: a fix line
            # that omits the scope sends the user to reinstall a different
            # file than the one this finding is about.
            lines.append(
                f"  Fix: remem install {report.agent} --scope {report.scope}"
            )
    return "\n".join(lines)


def to_dict(reports: list[AgentReport]) -> dict:
    """The --json form. A thin formatter over the same reports `render`
    reads, so the two can never disagree."""
    return {
        "failed": failed(reports),
        "agents": [
            {
                "agent": r.agent,
                "scope": r.scope,
                "path": str(r.path) if r.path else None,
                "examined": [str(p) for p in r.examined],
                "installed": r.installed,
                "verdict": str(r.verdict) if r.verdict else None,
                "warning": r.warning,
                "hooks": [
                    {
                        "event": f.event,
                        "verdict": str(f.verdict),
                        "required": f.required,
                        "provides": f.provides,
                        "detail": f.detail,
                    }
                    for f in r.findings
                ],
            }
            for r in reports
        ],
    }
