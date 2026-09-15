"""Importing another tool's knowledge store. Every policy decision is here.

Named `import_` because `import` is a keyword. The readers in
`saddlebag/importers/` know how to turn one source's rows into neutral records;
this module decides what a record becomes, how it is identified, and whether
a second run writes anything at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from saddlebag.domain import Entry, Kind, Origin, Query
from saddlebag.importers.base import SourceKind, SourceRecord
from saddlebag.services import write
from saddlebag.store import Store

#: What each source kind becomes. Nothing maps to Kind.RULE: a rule is a
#: convention this project agreed to follow, and no imported record is that.
#: Summaries are DOC and deliberately not origin=handoff - see the design.
KIND_FOR: dict[SourceKind, Kind] = {
    SourceKind.OBSERVATION: Kind.NOTE,
    SourceKind.MANUAL: Kind.NOTE,
    SourceKind.PROMPT: Kind.NOTE,
    SourceKind.SUMMARY: Kind.DOC,
}


@dataclass(frozen=True, slots=True)
class Planned:
    """One record, and what it will become. Produced without a store, so a
    dry run costs nothing and can be tested without Postgres."""

    record: SourceRecord
    kind: Kind
    project: str | None
    tags: list[str]


#: What `by_project` uses for a record whose project is `None` (every
#: legacy prompt group, since `user_prompts` has no project column). Without
#: this bucket those records simply vanish from the dict and the column
#: does not reconcile against the created/updated/unchanged total - the real
#: rehearsal printed 67 against 70 records and said nothing about where the
#: other three went.
NO_PROJECT: Final = "(no project)"


@dataclass(slots=True)
class Report:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    #: What project each record is filed under **after this run**, not what
    #: was planned for it. Those differ whenever `--project` is given on a
    #: second run: `write.supersede` (see `run()`) carries the *existing*
    #: entry's project forward unchanged, so a changed or unchanged record
    #: stays wherever it already was, no matter what `--project` says. This
    #: dict is built to match that reality - see `run()` - so the printed
    #: counts can never claim a move that did not happen.
    by_project: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False
    #: Rows `read()` could not map (an unrecognised `kind`), carried through
    #: untouched. `run()` does not call `read()` itself - the CLI reads,
    #: then hands this list in - so this field exists purely so the skip
    #: channel survives to whatever renders the Report, rather than dying
    #: at the boundary between reading and writing.
    skipped: list[str] = field(default_factory=list)


def identity_tag(namespace: str, source_id: str) -> str:
    """The tag that makes an import re-runnable, playing exactly the role
    `src:`/`sec:` plays for ingest."""
    return f"{namespace}:{source_id}"


def plan(
    records: list[SourceRecord],
    *,
    namespace: str,
    project: str | None = None,
) -> list[Planned]:
    """Decide what each record becomes. No store, no writes.

    `project` forces every record into one project. Without it the source's
    own project name is used verbatim: neither side derives the other, so
    the mapping is stated rather than guessed, and the dry run prints the
    distinct names before anything is created.
    """
    planned = []
    for record in records:
        planned.append(
            Planned(
                record=record,
                kind=KIND_FOR[record.kind],
                project=project or record.project,
                tags=[identity_tag(namespace, record.source_id), *record.tags],
            )
        )
    return planned


#: A source row is one entry, so a tag lookup should return one hit.
#: `_existing` raises `IdentityCollision` if it gets more than one, so the
#: limit itself only has to be large enough to see a collision when one
#: exists - it is not the collision threshold (that is `len(hits) > 1`,
#: unconditionally), just the query's bound.
MAX_PER_TAG: Final = 10


class ImportPreconditionFailed(Exception):
    """A check `run()` makes before writing anything failed.

    Named for what it means to the caller, not for the one cause it happens
    to be able to name (a schema behind the code): `_require_origin` raises
    this for a dropped connection or a permissions error too, and a name
    that only fit the schema case would tempt a future `except` clause into
    mishandling those. See `_require_origin` for how the message still
    tells the two apart."""


def _require_origin(store: Store, owner_id: UUID) -> None:
    """Probed once per import, before anything is written. A partial import
    that dies on its first row is worse than one that never starts."""
    try:
        store.search(Query(origins=[Origin.IMPORTED], limit=1), owner_id)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        # The most common cause is a schema behind the code, but a dropped
        # connection or permissions error is possible too. Name both the likely
        # cause and the actual error so the user is not misdirected if they are
        # staring at a different failure.
        raise ImportPreconditionFailed(
            f"could not probe the database (likely cause: migration 019 has not "
            f"been applied, which adds the 'imported' origin): {exc}\n"
            f"If the schema is current, check your database connection and try again. "
            f"Otherwise run `bag db up`."
        ) from exc


def run(
    store: Store,
    owner_id: UUID,
    records: list[SourceRecord],
    *,
    namespace: str,
    project: str | None = None,
    dry_run: bool = False,
    skipped: Sequence[str] = (),
) -> Report:
    """Import records, skipping what has not changed.

    `run()` does not call `read()` - the CLI reads, then passes
    `result.records` here positionally and `result.skipped` as `skipped`,
    so this function stays testable without touching whatever source a
    `ReadResult` came from.

    Fail-loud, like `ingest` and `embed` and unlike every hook here: a person
    typed this and is watching it.
    """
    _require_origin(store, owner_id)
    report = Report(dry_run=dry_run, skipped=list(skipped))
    for item in plan(records, namespace=namespace, project=project):
        kind_name = str(item.record.kind)
        report.by_kind[kind_name] = report.by_kind.get(kind_name, 0) + 1

        tag = identity_tag(namespace, item.record.source_id)
        existing = _existing(store, owner_id, tag)

        # What project the record is filed under once this run is done -
        # not what `--project` asked for. A create lands exactly where
        # planned; an update or a no-op does not move, because
        # `write.supersede` below carries the *existing* entry's project
        # forward untouched. Computing this here, from the same branch the
        # write itself takes, is what keeps the report from claiming a
        # move `run()` never makes - see the `by_project` docstring.
        actual_project = item.project if existing is None else existing.project
        project_key = actual_project or NO_PROJECT
        report.by_project[project_key] = report.by_project.get(project_key, 0) + 1

        if existing is None:
            report.created += 1
            if not dry_run:
                write.remember(
                    store,
                    owner_id,
                    title=item.record.title,
                    body=item.record.body,
                    summary=item.record.summary,
                    kind=item.kind,
                    project=item.project,
                    tags=item.tags,
                    origin=Origin.IMPORTED,
                )
        elif existing.body != item.record.body:
            report.updated += 1
            if not dry_run:
                # supersede, not store.set_superseded: the replacement is
                # exactly what we have. Ingest's orphan sweep calls the store
                # directly only because a deleted heading has none. supersede
                # carries tags, kind, project and origin across from the
                # EXISTING entry, not from `item` - so a `--project` given on
                # this run does not move an already-imported entry. See
                # "Importing claude-mem" in CLAUDE.md for the documented
                # limitation and what to do instead.
                write.supersede(
                    store,
                    owner_id,
                    existing.id,
                    title=item.record.title,
                    body=item.record.body,
                    summary=item.record.summary,
                )
        else:
            report.unchanged += 1
    return report


class IdentityCollision(Exception):
    """More than one live imported entry carries the same identity tag.

    One source row should map to one entry - `MAX_PER_TAG`'s docstring names
    this as the guard it exists for. A collision means two different rows
    (or two runs under different namespaces) minted the same tag, and
    picking one of the several live hits to supersede would be guessing
    which entry is really this row's history. Refuse instead: this is the
    same posture `store.set_superseded` takes toward an ambiguous `keep`."""


def _existing(store: Store, owner_id: UUID, tag: str) -> Entry | None:
    hits = store.search(
        Query(tags=[tag], origins=[Origin.IMPORTED], limit=MAX_PER_TAG),
        owner_id,
    )
    if not hits:
        return None
    if len(hits) > 1:
        raise IdentityCollision(
            f"{len(hits)} live entries carry the tag {tag!r}; expected at "
            f"most one. Resolve the collision by hand (see `bag dedupe "
            f"report`) before importing again."
        )
    return hits[0].entry
