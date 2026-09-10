"""Importing another tool's knowledge store. Every policy decision is here.

Named `import_` because `import` is a keyword. The readers in
`remem/importers/` know how to turn one source's rows into neutral records;
this module decides what a record becomes, how it is identified, and whether
a second run writes anything at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from remem.domain import Entry, Kind, Origin, Query
from remem.importers.base import SourceKind, SourceRecord
from remem.services import write
from remem.store import Store

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


@dataclass(slots=True)
class Report:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
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


#: A source row is one entry, so a tag lookup should return one hit. The cap
#: is a guard against a namespace collision silently superseding the wrong
#: entry, not a real expectation.
MAX_PER_TAG: Final = 10


class SchemaTooOld(Exception):
    """The database has no `imported` origin, so migration 019 has not been
    applied. Raised instead of letting psycopg surface an enum error naming a
    type the user has never heard of."""


def _require_origin(store: Store, owner_id: UUID) -> None:
    """Probed once per import, before anything is written. A partial import
    that dies on its first row is worse than one that never starts."""
    try:
        store.search(Query(origins=[Origin.IMPORTED], limit=1), owner_id)
    except Exception as exc:  # noqa: BLE001 - re-raised as a named error
        raise SchemaTooOld(
            "this database has no 'imported' origin, so migration 019 has not "
            "been applied. Run `remem db up`, then import again."
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
        if item.project:
            report.by_project[item.project] = report.by_project.get(item.project, 0) + 1

        tag = identity_tag(namespace, item.record.source_id)
        existing = _existing(store, owner_id, tag)
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
                # carries tags, kind, project and origin across, which is what
                # keeps the identity tag alive for the next run.
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


def _existing(store: Store, owner_id: UUID, tag: str) -> Entry | None:
    hits = store.search(
        Query(tags=[tag], origins=[Origin.IMPORTED], limit=MAX_PER_TAG),
        owner_id,
    )
    if not hits:
        return None
    return hits[0].entry
