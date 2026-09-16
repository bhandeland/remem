"""Parsing a Claude Code transcript, and deciding how to re-read one.

Pure: no I/O, no store, no filesystem. That is what lets this file's tests
run on CI, where there is no Postgres and no `~/.claude` - the same bargain
`markdown.py`, `memory_file.py` and `session_size.py` make.

The caller does every read; this module is handed bytes and sizes and
returns decisions and rows.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from saddlebag.domain import TranscriptLine

__all__ = [
    "LineFailure",
    "ReadPlan",
    "classify",
    "parse",
    "sha256_hex",
]


class ReadPlan(StrEnum):
    """What an import should do with a file it has seen before."""

    SKIP = "skip"
    APPEND = "append"
    REBUILD = "rebuild"
    SHRUNK = "shrunk"


@dataclass
class LineFailure:
    """A line that could not become a row, named rather than dropped."""

    seq: int
    reason: str


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def classify(
    stored_bytes: int,
    stored_sha: str,
    disk_size: int,
    disk_prefix_sha: str | None,
) -> ReadPlan:
    """Decide how to re-read a transcript, from sizes and hashes alone.

    Pure so that all four branches are testable without a filesystem, and
    ordered so the cheap answer comes first: an unchanged size is the common
    case at every session start, and it must cost one stat and no read. The
    caller only computes `disk_prefix_sha` - which needs reading
    `stored_bytes` bytes - when the size grew.

    A file that SHRANK is the one case where the file on disk is not
    followed. The stored copy is more complete, and two sessions are already
    gone from disk: the purpose of the source row is that a rotating file
    does not destroy the session.
    """
    if disk_size == stored_bytes:
        return ReadPlan.SKIP
    if disk_size < stored_bytes:
        return ReadPlan.SHRUNK
    if disk_prefix_sha is not None and disk_prefix_sha == stored_sha:
        return ReadPlan.APPEND
    # The prefix changed, so the file was rewritten rather than appended to.
    # Cheap to recover from and safe by construction: the lines are derived
    # and hold nothing of their own.
    return ReadPlan.REBUILD


def parse(
    content: bytes, start_seq: int = 0
) -> tuple[list[TranscriptLine], list[LineFailure]]:
    """Split JSONL into rows, accounting for every line.

    The invariant, and the one the test asserts: rows written plus failures
    recorded equals the number of non-empty lines. A line whose SHAPE is
    unrecognised becomes a row with a null `type` - Claude Code's format is
    not ours and will change, and an import that fails on an unfamiliar line
    would strand a whole session. A line that is not valid JSON cannot become
    a `jsonb` row at all, so it is named as a failure instead. Neither is
    ever silently dropped.

    `start_seq` exists for the append path, which parses only the tail: seq
    is the line's number within the FILE, not within this call, because
    (transcript_id, seq) is the coordinate labels will reference and it has
    to mean the same thing after an append as before one.

    Blank lines are not lines. A trailing newline must not become a null row.
    """
    lines: list[TranscriptLine] = []
    failures: list[LineFailure] = []

    for offset, raw_line in enumerate(content.split(b"\n")):
        if not raw_line.strip():
            continue
        seq = start_seq + offset
        try:
            # Decoding and parsing in one try: invalid UTF-8 and invalid JSON
            # are the same outcome here - a line that cannot be a jsonb row -
            # and splitting them would only give two names to one recovery.
            payload = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            failures.append(LineFailure(seq=seq, reason=f"line {seq}: {exc}"))
            continue
        if not isinstance(payload, dict):
            # A bare string or list is valid JSON and not a transcript line.
            # It stores as raw would be ambiguous, so it is a named failure.
            failures.append(
                LineFailure(seq=seq, reason=f"line {seq}: not a JSON object")
            )
            continue
        if _holds_nul(payload):
            # Valid JSON that `jsonb` refuses: `\u0000` decodes to U+0000,
            # which Postgres text cannot hold. Found by a real import, where
            # the refused batch raised out of the whole run - and because
            # the content had already committed with zero lines, the
            # zero-lines repair re-parsed the file on every later run and
            # stopped the project's imports for good. Same outcome as
            # invalid JSON: no row, a named failure, bytes kept in source.
            failures.append(
                LineFailure(
                    seq=seq, reason=f"line {seq}: holds U+0000, which jsonb refuses"
                )
            )
            continue
        lines.append(
            TranscriptLine(
                seq=seq,
                type=_text(payload.get("type")),
                uuid=_text(payload.get("uuid")),
                occurred_at=_timestamp(payload.get("timestamp")),
                raw=payload,
            )
        )

    return lines, failures


def _holds_nul(value: Any) -> bool:
    """Whether any key or string anywhere in a parsed line holds U+0000."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_holds_nul(k) or _holds_nul(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_holds_nul(v) for v in value)
    return False


def _text(value: Any) -> str | None:
    """A hoisted field, only when it really is text.

    A `type` that arrives as a number is a format change, not a crash: the
    value stays in `raw` and the hoisted column goes null.
    """
    return value if isinstance(value, str) else None


def _timestamp(value: Any) -> datetime | None:
    """Claude Code's ISO-8601 timestamp, or None.

    Nullable for the same reason `type` is: this column exists for indexing
    and nothing depends on it, so an unrecognised shape must cost the index
    entry and never the line. `Z` is spelled out because `fromisoformat`
    accepts it only from 3.11 onward and being explicit costs nothing.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
