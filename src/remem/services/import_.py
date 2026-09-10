"""Importing another tool's knowledge store. Every policy decision is here.

Named `import_` because `import` is a keyword. The readers in
`remem/importers/` know how to turn one source's rows into neutral records;
this module decides what a record becomes, how it is identified, and whether
a second run writes anything at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from remem.domain import Kind
from remem.importers.base import SourceKind, SourceRecord

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
