"""Turning a session's recorded events into candidate entries.

The validator here does not trust the model. Everything an extractor returns
is untrusted text: it gets shape-checked, capped, and filtered before any of
it reaches the store. That contract is unchanged by where the input comes
from - only the input changed, from a transcript file to a list of event
rows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from remem.domain import Event, Kind

MAX_ENTRIES = 5
MAX_TITLE = 200
MAX_BODY = 4000

# Set on the environment of a spawned `claude -p` extraction child so its own
# hooks can detect they're running inside an extraction and refuse to recurse.
# Defined here (not in the hook module) because base.py is layer-neutral: both
# the extractor and the agent hook can import it without depending on each
# other. This string MUST match wherever the child process checks it.
CHILD_ENV_VAR = "REMEM_CAPTURE_CHILD"


class ExtractionFailed(Exception):
    """The extractor produced output that could not be read as entries.

    `raw` carries the model's actual output where we have it. A user cannot
    fix a prompt whose failing response they never see, so the extraction
    run records it against the job.
    """

    def __init__(self, message: str, raw: str | None = None):
        super().__init__(message)
        self.raw = raw


@dataclass(slots=True)
class ExtractedEntry:
    title: str
    body: str
    kind: Kind
    tags: list[str] = field(default_factory=list)


class Extractor(Protocol):
    def extract(
        self,
        events: list[Event],
        project: str,
        known_titles: list[str] | None = None,
    ) -> list[ExtractedEntry]:
        """Extract durable entries from one session's events, oldest first.

        `known_titles` is what this project already holds - the extractor is
        expected not to re-record them. Optional so a simpler implementation
        can ignore it; the caller checks the signature before passing it.
        """
        ...


def _clean_tags(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [t.strip() for t in value if isinstance(t, str) and t.strip()]


def _entry_from(item: object) -> ExtractedEntry | None:
    """One malformed entry drops itself rather than failing the whole batch."""
    if not isinstance(item, dict):
        return None
    title = item.get("title")
    body = item.get("body")
    if not isinstance(title, str) or not isinstance(body, str):
        return None
    if not title.strip() or not body.strip():
        return None
    try:
        kind = Kind(item.get("kind", "note"))
    except ValueError:
        return None
    return ExtractedEntry(
        title=title.strip()[:MAX_TITLE],
        body=body.strip()[:MAX_BODY],
        kind=kind,
        tags=_clean_tags(item.get("tags")),
    )


def _find_array_candidates(raw: str) -> list[list[object]]:
    """Scan for every top-level JSON array embedded in `raw`.

    A greedy "first [ to last ]" regex would span from a decoy array clear
    through to the real one (or past a stray "]" in trailing prose), and
    then fail to parse as JSON at all. Instead we try `raw_decode` at every
    "[" and keep whatever actually decodes as a list, in the order they
    appear. Non-list top-level values (e.g. `{}` decoding at a `[` inside
    a fenced block's sibling text) and parse failures at a given position
    are simply skipped - they aren't array candidates.
    """
    decoder = json.JSONDecoder()
    candidates: list[list[object]] = []
    for i, ch in enumerate(raw):
        if ch != "[":
            continue
        try:
            value, _ = decoder.raw_decode(raw, i)
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            candidates.append(value)
    return candidates


def parse_entries(raw: str) -> list[ExtractedEntry]:
    """Read a extractor's raw output into validated entries.

    Accepts a bare JSON array, or one embedded in prose or a fenced block -
    models prepend explanations however firmly the prompt asks them not to.
    Models also second-guess themselves mid-output ("attempt 1 was wrong,
    here's the real one") or drop a stray "[0]" into trailing prose, so
    there can be more than one JSON array in the text. We take the FIRST
    array that yields at least one valid entry - not merely the first one
    that parses - because the first parseable array might be a decoy like
    `[1,2,3]` that itself contains no usable entries.

    Raises ExtractionFailed when no JSON array can be found at all.

    An empty array ("[]") is a deliberate, successful "found nothing" result
    - most sessions contain nothing durable - and returns []. But when every
    candidate array is non-empty and none of them yields a valid entry, that's
    different: the model produced output, and none of it was usable. That's a
    extraction failure, not a quiet clean session, so it raises rather than
    silently returning [] - it needs to show up as a failed job instead of
    being indistinguishable from a week with nothing worth extracting.
    """
    candidates = _find_array_candidates(raw or "")
    if not candidates:
        raise ExtractionFailed("no JSON array in extractor output", raw)

    saw_empty = False
    for data in candidates:
        if not data:
            saw_empty = True
            continue
        entries = [e for e in (_entry_from(i) for i in data) if e is not None]
        if entries:
            return entries[:MAX_ENTRIES]

    if saw_empty:
        return []
    raise ExtractionFailed(
        "no candidate array contained a valid entry", raw
    )
