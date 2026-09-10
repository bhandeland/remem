"""Reading a claude-mem sqlite database. Pure: a path in, records out.

No store, no policy, no network. The one file it is given is the only thing
it touches, which is what lets its tests build a database in tmp_path and
carry no `db` marker.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from remem.importers.base import SourceKind, SourceRecord

#: Prefixes every tag this importer mints, so an Obsidian import can never
#: collide with a claude-mem one on identity.
NAMESPACE: Final = "cmem"

#: Named in the refusal message when neither schema is present, so a person
#: pointed at the wrong file learns what was actually expected.
MODERN_TABLE: Final = "memory_items"
LEGACY_TABLES: Final = ("observations", "session_summaries", "user_prompts")


class UnreadableSource(Exception):
    """The file is not a claude-mem database, or not a database at all."""


def read(path: Path) -> list[SourceRecord]:
    conn = _open(path)
    try:
        tables = _tables(conn)
        if MODERN_TABLE in tables:
            return _read_modern(conn)
        raise UnreadableSource(
            f"{path} has none of the tables a claude-mem database has. "
            f"Looked for {MODERN_TABLE!r} (schema 33 and later) and "
            f"{', '.join(repr(t) for t in LEGACY_TABLES)} (earlier)."
        )
    finally:
        conn.close()


def _open(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        # connect() is lazy, so a non-database file is only discovered on
        # the first read. Force it here, where the path is still in hand.
        conn.execute("select count(*) from sqlite_master")
    except sqlite3.Error as exc:
        raise UnreadableSource(f"{path} is not readable as sqlite: {exc}") from exc
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("select name from sqlite_master where type='table'")
    return {r[0] for r in rows}


def _read_modern(conn: sqlite3.Connection) -> list[SourceRecord]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "select m.*, p.name as project_name "
        "from memory_items m left join projects p on p.id = m.project_id "
        "order by m.created_at_epoch"
    )
    return [_record(row) for row in rows]


def _record(row: sqlite3.Row) -> SourceRecord:
    kind = SourceKind(row["kind"])
    return SourceRecord(
        source_id=str(row["id"]),
        kind=kind,
        project=row["project_name"],
        title=row["title"] or "(untitled)",
        summary=(row["subtitle"] or None),
        body=_body(row),
        tags=(f"{NAMESPACE}-type:{row['type']}",) if row["type"] else (),
        created_at=_when(row["created_at_epoch"]),
    )


def _body(row: sqlite3.Row) -> str:
    """Prose, not JSON. The body is half the embedding text and the bulk of
    the tsvector, so a serialised array here would be searched as punctuation.
    """
    parts: list[str] = []
    for field in ("narrative", "text"):
        value = (row[field] or "").strip()
        if value:
            parts.append(value)
    for field, heading in (("facts", "Facts"), ("concepts", "Concepts")):
        items = _json_list(row[field])
        if items:
            bullets = "\n".join(f"- {item}" for item in items)
            parts.append(f"## {heading}\n\n{bullets}")
    return "\n\n".join(parts)


def _json_list(raw: str | None) -> list[str]:
    """claude-mem stores these as a JSON array in a TEXT column, and the
    2026-06-30 export proves they are sometimes a JSON string holding a JSON
    array. Anything that does not decode into a list is dropped rather than
    raised on: one malformed column must not end an import."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except TypeError, ValueError:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except TypeError, ValueError:
            return [value]
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if str(v).strip()]


def _when(epoch: int | None) -> datetime | None:
    """claude-mem writes epoch milliseconds in some columns and seconds in
    others across versions. Values past the year 2200 are milliseconds."""
    if not epoch:
        return None
    seconds = epoch / 1000 if epoch > 7_258_118_400 else epoch
    return datetime.fromtimestamp(seconds, tz=timezone.utc)
