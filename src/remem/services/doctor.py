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

from remem.agents.base import HookState


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
    agent: str
    event: str
    verdict: Verdict
    required: bool
    provides: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AgentReport:
    agent: str
    #: The config file examined, or None - either because the adapter has no
    #: hook configuration or because the file does not exist.
    path: Path | None = None
    #: An adapter is INSTALLED when at least one remem command appears in
    #: its config. Zero is "not installed" and stays quiet; some-but-not-all
    #: is the bug this module exists to catch and is never quiet.
    installed: bool = False
    findings: tuple[Finding, ...] = ()
    #: Set only when the whole adapter could not be examined.
    verdict: Verdict | None = None
    warning: str | None = None


def _judge(agent: str, state: HookState) -> tuple[Finding, ...]:
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
            agent=agent, event=hook.event, verdict=verdict,
            required=hook.required, provides=hook.provides, detail=detail,
        ))
    return tuple(findings)


def check(
    adapters: Mapping[str, object],
    scope: str = "user",
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[AgentReport]:
    """One report per adapter, in name order.

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
        try:
            state = state_fn(scope, home, env)
        except Exception as exc:
            reports.append(AgentReport(
                agent=name, verdict=Verdict.UNCHECKED,
                warning=f"could not read {name}'s hook configuration: {exc}",
            ))
            continue

        installed = any(state.found.get(h.event) for h in state.expected)
        reports.append(AgentReport(
            agent=name,
            path=state.path,
            installed=installed,
            findings=_judge(name, state) if installed else (),
        ))
    return reports


def failed(reports: list[AgentReport]) -> bool:
    """True when a required hook is missing on any adapter.

    Only MISSING-and-required fails. STALE and DUPLICATED both still fire
    the hook, and UNCHECKED reports the absence of a check rather than the
    presence of a fault - exiting non-zero for "I could not tell" would
    train users to ignore the exit code, and the exit code is the half a
    script reads.
    """
    return any(
        f.verdict is Verdict.MISSING and f.required
        for r in reports for f in r.findings
    )


def advisories(reports: list[AgentReport]) -> list[str]:
    """One line per adapter whose install is incomplete, for `record
    status` - which is what a user runs when a harness looks quiet, and is
    otherwise structurally incapable of answering."""
    lines: list[str] = []
    for report in reports:
        broken = [f for f in report.findings if f.verdict is not Verdict.OK]
        if not broken:
            continue
        named = ", ".join(f"{f.event} ({f.verdict})" for f in broken)
        lines.append(
            f"{report.agent} is installed but its hooks are incomplete: "
            f"{named} - run `remem doctor {report.agent}`"
        )
    return lines


def render(reports: list[AgentReport]) -> str:
    lines: list[str] = []
    for report in reports:
        if report.verdict is Verdict.UNCHECKED:
            lines.append(f"{report.agent}: no hook registration to check")
            if report.warning:
                lines.append(f"  {report.warning}")
            continue
        if not report.installed:
            lines.append(f"{report.agent}: not installed")
            continue
        lines.append(f"{report.agent}  {report.path}")
        for f in report.findings:
            mark = "ok" if f.verdict is Verdict.OK else f.verdict.upper()
            note = f"  - {f.detail or f.provides}" if f.verdict is not Verdict.OK else ""
            lines.append(f"  {f.event:<18} {mark}{note}")
        if any(f.verdict is not Verdict.OK for f in report.findings):
            lines.append(f"  Fix: remem install {report.agent}")
    return "\n".join(lines)


def to_dict(reports: list[AgentReport]) -> dict:
    """The --json form. A thin formatter over the same reports `render`
    reads, so the two can never disagree."""
    return {
        "failed": failed(reports),
        "agents": [
            {
                "agent": r.agent,
                "path": str(r.path) if r.path else None,
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
