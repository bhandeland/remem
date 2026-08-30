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

from remem.config import load as load_config
from remem.domain import DuplicateGroup, ExtractJob, HarnessStats, ProvenanceRow
from remem.services import extraction
from remem.store import Store

# How many awaiting-extraction sessions `status` will look at when tallying
# `sessions_awaiting` per harness. `process`'s own limit is a batch size -
# how much work to *do* per run - and bounding `status` the same way would
# make a large backlog undercount itself in the one place meant to reveal
# it. Generous rather than unbounded: remem's scale is tens of sessions
# between prunes, not thousands, so a four-figure ceiling costs nothing in
# practice while still refusing to scan forever for a pathological owner.
STATUS_AWAITING_LIMIT = 1000

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


# ---------------- status: making silence visible ----------------


@dataclass(slots=True)
class StatusReport:
    """Everything `remem record status` shows, gathered in one read-only
    pass.

    Bundled here rather than assembled piecemeal in the CLI so that `--json`
    and the human-readable form can never disagree about the numbers: both
    `render` and `to_dict` are thin formatters over the same report, and
    neither one computes anything the other doesn't see.
    """

    harnesses: list[HarnessStats]
    enabled_projects: list[str]
    job_counts: dict[str, int]
    recent_failures: list[ExtractJob]
    legacy_pending: int
    extract_model: str
    #: Repeated events 011's unique index cannot reach - see
    #: `store.duplicate_unkeyed_events`. Advisory: nothing acts on it.
    suspected_duplicates: list[DuplicateGroup]


def status(store: Store, owner_id: UUID, idle_seconds: int) -> StatusReport:
    """Gather the numbers behind `remem record status`. Read-only.

    `extract_model` is resolved from `config.load()` rather than taken as a
    parameter: it is the same value every command that reports it (this one,
    and the older `capture status`) would resolve, and a caller with no
    `Config` object handy - a test, an MCP tool - still gets a real answer
    instead of a required argument nothing passes.

    `sessions_awaiting` per harness is filled in here, after the store call,
    by tallying `extraction.awaiting_sessions` - not computed in SQL inside
    `event_stats`. That function is the one place the "has this session's
    extraction given up" rule is evaluated (`extraction._gave_up`); this
    used to be duplicated as a second, hand-copied SQL predicate here, which
    is exactly the kind of silent disagreement this whole command exists to
    catch, one level up. The cost is one `extract_job_for_session` lookup
    per candidate session, bounded by `STATUS_AWAITING_LIMIT` - at remem's
    scale (tens of sessions) that is nothing.
    """
    harnesses = store.event_stats(owner_id)
    awaiting_counts: dict[str, int] = {}
    for session in extraction.awaiting_sessions(
        store, owner_id, idle_seconds, STATUS_AWAITING_LIMIT
    ):
        awaiting_counts[session.harness] = awaiting_counts.get(session.harness, 0) + 1
    for h in harnesses:
        h.sessions_awaiting = awaiting_counts.get(h.harness, 0)

    return StatusReport(
        harnesses=harnesses,
        enabled_projects=store.enabled_record_projects(owner_id),
        job_counts=store.extract_job_counts(owner_id),
        recent_failures=store.recent_failed_extract_jobs(owner_id),
        legacy_pending=store.pending_legacy_capture_jobs(owner_id),
        extract_model=load_config().extract_model,
        suspected_duplicates=store.duplicate_unkeyed_events(owner_id),
    )


def render(report: StatusReport) -> str:
    """Human-readable `remem record status`. See `to_dict` for `--json` -
    both read off the same `StatusReport`, so they cannot disagree about
    what "awaiting" or "no events" means.
    """
    lines = [
        "Recording enabled for: "
        + (", ".join(report.enabled_projects) or "no projects"),
        f"Extraction model: {report.extract_model}",
    ]
    if not report.harnesses:
        # This is the failure the whole command exists for: an adapter
        # bound to hook names its harness never emits records nothing and
        # looks exactly like a quiet day. Say so explicitly rather than
        # leaving the harness section out - absence must not be how "no
        # events" is displayed.
        lines.append("no events recorded from any harness yet")
    else:
        for h in report.harnesses:
            last = h.last_event_at.isoformat() if h.last_event_at else "never"
            lines.append(
                f"  {h.harness}: {h.events_24h} event(s) in the last 24h "
                f"(last at {last}), {h.sessions_awaiting} session(s) "
                f"awaiting extraction"
            )
    if report.job_counts:
        lines.append(
            "Jobs: "
            + ", ".join(f"{k}={v}" for k, v in sorted(report.job_counts.items()))
        )
    else:
        lines.append("Jobs: none yet")
    for f in report.recent_failures:
        lines.append(f"  failed {f.id} [{f.project}]: {f.error}")
    for d in report.suspected_duplicates:
        # Named with the fix, not just the count. The cause is almost always
        # a hook registered twice - which is what re-running the install
        # repairs - and a number on its own leaves the user to work that
        # out from a report they were not looking for.
        lines.append(
            f"  possible duplicate: {d.count} identical "
            f"{d.harness} event(s) in session "
            f"{d.session_id} [{d.project}] - a hook is likely registered "
            f"twice; re-run `remem install {d.harness}`"
        )
    if report.legacy_pending:
        lines.append(
            f"{report.legacy_pending} job(s) stranded in the retired "
            "capture_jobs_legacy table - see 010_retire_capture_jobs.sql"
        )
    return "\n".join(lines)


def to_dict(report: StatusReport) -> dict:
    """The `--json` half of `render`. Same `StatusReport`, same numbers."""
    return {
        "extract_model": report.extract_model,
        "enabled_projects": report.enabled_projects,
        "harnesses": [
            {
                "harness": h.harness,
                "events_24h": h.events_24h,
                "last_event_at": (
                    h.last_event_at.isoformat() if h.last_event_at else None
                ),
                "sessions_awaiting": h.sessions_awaiting,
            }
            for h in report.harnesses
        ],
        "job_counts": report.job_counts,
        "recent_failures": [
            {"id": str(f.id), "project": f.project, "error": f.error}
            for f in report.recent_failures
        ],
        "legacy_pending": report.legacy_pending,
        "suspected_duplicates": [
            {
                "project": d.project,
                "harness": d.harness,
                "session_id": d.session_id,
                "count": d.count,
            }
            for d in report.suspected_duplicates
        ],
    }


# ---------------- forensics: the entry -> events lookup ----------------


def forensics(store: Store, owner_id: UUID, entry_id: UUID) -> list[ProvenanceRow]:
    """The forensic lookup behind `remem events show`.

    `Store.provenance` returns bare 4-tuples - Task 7's tests assert against
    that shape directly, so its signature is left alone - and this is the
    domain boundary that turns them into a named type before they cross into
    a frontend.
    """
    return [
        ProvenanceRow(event_id=eid, session_id=session_id, harness=harness,
                      present=present)
        for eid, session_id, harness, present in store.provenance(entry_id, owner_id)
    ]


def render_provenance(rows: list[ProvenanceRow]) -> str:
    """`remem events show` output.

    No rows at all is an ordinary answer, not an error: a hand-written entry
    has no events and never will. A row with `present=False` says "event
    pruned", never "not found" - "we recorded where this came from and then
    deleted the raw" and "we never recorded anything" are different facts,
    and a user who cannot tell them apart concludes provenance was never
    recorded at all.
    """
    if not rows:
        return "no events recorded for this entry (written directly, not extracted)"
    lines = []
    for row in rows:
        note = "" if row.present else " - event pruned"
        lines.append(f"  {row.event_id} [{row.harness}/{row.session_id}]{note}")
    return "\n".join(lines)
