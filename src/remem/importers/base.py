"""What every importer produces, regardless of what it read.

Neutral on purpose: the claude-mem reader is the first producer and the
Obsidian reader is the second, and the service that maps these into entries
must not learn either source's vocabulary. A record carries a body that is
already rendered prose, because HOW a source's fields become prose is that
source's concern - what the service decides is kind, origin, identity and
idempotency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SourceKind(StrEnum):
    """What the source called this record, before remem decides what it is."""

    OBSERVATION = "observation"
    SUMMARY = "summary"
    PROMPT = "prompt"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class SourceRecord:
    #: Stable within the source. Becomes the `<ns>:<source_id>` identity tag,
    #: which is what makes an import re-runnable.
    source_id: str
    kind: SourceKind
    project: str | None
    title: str
    summary: str | None
    body: str
    #: Source-specific extras, already namespaced (e.g. `cmem-type:discovery`).
    #: Never prose - see the reader's rendering rules.
    tags: tuple[str, ...] = ()
    created_at: datetime | None = None
