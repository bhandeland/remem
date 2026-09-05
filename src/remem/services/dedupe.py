"""Finding entries that say the same thing twice.

Every judgement in this feature lives here: what counts as near, which tier
a pair belongs to, which member is suggested for keeping, and what an
unembedded population means. The store answers questions; the frontends
print. See docs/superpowers/specs/2026-09-05-dedupe-design.md.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from remem.domain import (
    DedupeReport,
    DuplicateSet,
    Entry,
    NearPair,
    Origin,
    Query,
)
from remem.store import Store

#: How similar two entries must be to be worth a human look.
#:
#: NOT `config.semantic_threshold`, which is a search-recall floor - how
#: loose a match is still worth showing someone who asked a question.
#: Sameness is a different and much higher bar, and sharing the knob would
#: mean tuning recall silently retunes what counts as a duplicate.
DEFAULT_THRESHOLD = 0.95

#: Near pairs shown by default. The exact tier is deliberately unbounded;
#: pairs grow with the square of the population, so they need a bound.
DEFAULT_PAIR_LIMIT = 50

#: Origin trust, highest first, for suggesting which member to keep. A
#: written-by-hand entry outranks the extractor's version of the same fact,
#: which is one of the two duplication sources this report exists to find.
_ORIGIN_RANK = {
    Origin.HUMAN: 0,
    Origin.AGENT: 1,
    Origin.HANDOFF: 2,
    Origin.INGESTED: 3,
    Origin.ARCHIVED: 4,
    Origin.EXTRACTED: 5,
}

_EPOCH = datetime.fromtimestamp(0, tz=timezone.utc)


def survivor(entries: list[Entry]) -> Entry:
    """The member this report suggests keeping.

    A heuristic, and allowed to be wrong: it is printed as a line to run,
    edit or ignore, never applied. Trust first, then recency, then id - the
    last so that two runs over unchanged data print the same line.
    """
    return min(
        entries,
        key=lambda e: (
            _ORIGIN_RANK.get(e.origin, len(_ORIGIN_RANK)),
            -(e.updated_at or _EPOCH).timestamp(),
            str(e.id),
        ),
    )


def suppress(
    near: list[NearPair], exact: list[DuplicateSet]
) -> list[NearPair]:
    """Drop near pairs whose members already share an exact group.

    Both tiers always run - this is a report, not a lookup, so suppressing
    the near tier wholesale because the exact tier found something would
    hide most of the answer. They still never blend: a pair reported as an
    identical body is not reported a second time with a score.
    """
    grouped = {frozenset(e.id for e in s.entries) for s in exact}
    return [
        p for p in near
        if not any({p.a.id, p.b.id} <= ids for ids in grouped)
    ]


def report(
    store: Store,
    owner_id: UUID,
    query: Query,
    model: str,
    threshold: float = DEFAULT_THRESHOLD,
    limit: int = DEFAULT_PAIR_LIMIT,
) -> DedupeReport:
    """Both tiers plus coverage, in one pass.

    No embedder is built, ever. Constructing a LocalEmbedder imports
    fastembed, builds an ONNX session and can download ~130MB, and it fails
    outright where the optional extra is not installed. A report must not do
    either. The cost of that is partial coverage, which is why coverage is
    always rendered.
    """
    exact = store.exact_duplicate_groups(query, owner_id)
    pairs, near_total = store.near_duplicate_pairs(
        query, owner_id, model, threshold, limit
    )
    kept = suppress(pairs, exact)
    embedded, total = store.vector_coverage(query, owner_id, model)
    return DedupeReport(
        exact=exact,
        near=kept,
        # The store's count above the threshold, before suppression and
        # before truncation. Recomputing it after suppression would need the
        # whole untruncated set, which is exactly what the limit avoids
        # fetching - so the renderer is told how many were suppressed and
        # whether anything was truncated, and says the two separately.
        near_total=near_total,
        threshold=threshold,
        model=model,
        embedded=embedded,
        total=total,
        near_suppressed=len(pairs) - len(kept),
        near_truncated=near_total > len(pairs),
    )


def _line(entry: Entry) -> str:
    when = entry.updated_at.date().isoformat() if entry.updated_at else "-"
    return (f"    {entry.id}  [{entry.kind}] {entry.title}"
            f"  {entry.origin}  {when}")


def _resolve_lines(members: list[Entry]) -> list[str]:
    keep = survivor(members)
    return [
        f"    remem dedupe resolve {e.id} --keep {keep.id}"
        for e in members if e.id != keep.id
    ]


def render(report: DedupeReport) -> str:
    """The human rendering. The only place this output's text is decided."""
    out: list[str] = []

    if report.exact:
        entries = sum(len(s.entries) for s in report.exact)
        out.append(f"Exact duplicates: {len(report.exact)} groups, "
                   f"{entries} entries")
        for group in report.exact:
            out.append("")
            out.append(f"  identical body, {len(group.entries)} entries")
            out.extend(_line(e) for e in group.entries)
            out.extend(_resolve_lines(group.entries))
    else:
        out.append("Exact duplicates: none.")

    out.append("")

    if report.embedded == 0:
        # Never an empty near section with no explanation: that reads as a
        # clean bill of health for a tier that never ran. Same rule as
        # `remem doctor` - unchecked never renders as ok.
        out.append(
            f"Near-duplicates: not checked - no entries carry a vector for "
            f"{report.model}.\n  Run `remem embed` to enable this tier."
        )
    elif report.near:
        out.append(f"Near-duplicates: {len(report.near)} pairs at "
                   f">= {report.threshold}")
        # Suppressed and truncated are separate sentences. A single
        # "showing N of M" covering both tells a reader whose list was
        # merely deduplicated that their output was cut short.
        if report.near_suppressed:
            out.append(f"  {report.near_suppressed} further pairs are listed "
                       f"above as identical bodies.")
        if report.near_truncated:
            out.append(f"  {report.near_total} pairs are above the "
                       f"threshold - raise --limit to see more.")
        for pair in report.near:
            out.append("")
            out.append(f"  {pair.similarity:.3f}")
            out.extend(_line(e) for e in (pair.a, pair.b))
            out.extend(_resolve_lines([pair.a, pair.b]))
    else:
        out.append(f"No near-duplicates at >= {report.threshold}.")

    out.append("")
    out.append(f"Vector coverage: {report.embedded} of {report.total} "
               f"entries embedded for {report.model}.")
    missing = report.total - report.embedded
    if missing:
        out.append(f"  {missing} entries were not compared - "
                   f"run `remem embed`.")
    return "\n".join(out)


class CannotResolve(Exception):
    """A resolve that would rewrite history or point at a tombstone.

    Fail-loud, unlike the report and unlike every hook in this repository,
    for the reason `remem ingest` and `remem handoff write` are: a person is
    standing there having asked for it.
    """


def resolve(
    store: Store, owner_id: UUID, drop_id: UUID, keep_id: UUID
) -> tuple[Entry, Entry]:
    """Point `drop` at `keep`, both of which already exist.

    This is not `write.supersede`, which requires a title and mints a NEW
    entry for knowledge that stopped being true. Here both entries exist and
    one of them is redundant, so the primitive is `store.set_superseded` -
    the same one the ingest orphan sweep calls directly, and for the same
    reason: there is no replacement to mint.
    """
    if drop_id == keep_id:
        raise CannotResolve("an entry cannot supersede itself")

    drop = store.get_entry(drop_id, owner_id)
    if drop is None:
        raise CannotResolve(f"no entry {drop_id}")
    keep = store.get_entry(keep_id, owner_id)
    if keep is None:
        raise CannotResolve(f"no entry {keep_id}")

    if drop.superseded_by is not None:
        raise CannotResolve(
            f"{drop_id} is already superseded by {drop.superseded_by}"
        )
    if keep.superseded_by is not None:
        raise CannotResolve(
            f"{keep_id} is itself superseded by {keep.superseded_by} - "
            "resolve into the entry that is still live"
        )

    store.set_superseded(drop_id, keep_id, owner_id)
    return drop, keep
