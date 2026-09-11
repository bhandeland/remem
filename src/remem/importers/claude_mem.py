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
from urllib.parse import quote

from remem.importers.base import ReadResult, SourceKind, SourceRecord

#: Prefixes every tag this importer mints, so an Obsidian import can never
#: collide with a claude-mem one on identity.
NAMESPACE: Final = "cmem"

#: Named in the refusal message when neither schema is present, so a person
#: pointed at the wrong file learns what was actually expected.
MODERN_TABLE: Final = "memory_items"
LEGACY_TABLES: Final = ("observations", "session_summaries", "user_prompts")


class UnreadableSource(Exception):
    """The file is not a claude-mem database, or not a database at all."""


def read(path: Path) -> ReadResult:
    conn = _open(path)
    try:
        tables = _tables(conn)
        if MODERN_TABLE in tables:
            result = _read_modern(conn)
            # `memory_items.legacy_observation_id` is direct evidence that a
            # v33 database can be one migrated in place from the pre-33
            # shape - claude-mem's own migration is not guaranteed to have
            # dropped the old tables. Reading both risks double-importing
            # rows the migration already copied into `memory_items`, so
            # these are named rather than read: silence is the one outcome
            # `skipped` exists to prevent, and it is worse than either
            # choice here.
            leftover = set(LEGACY_TABLES) & tables
            if leftover:
                result.skipped.extend(_unread_legacy_counts(conn, leftover))
            return result
        if set(LEGACY_TABLES) & tables:
            return _read_legacy(conn, tables)
        raise UnreadableSource(
            f"{path} has none of the tables a claude-mem database has. "
            f"Looked for {MODERN_TABLE!r} (schema 33 and later) and "
            f"{', '.join(repr(t) for t in LEGACY_TABLES)} (earlier)."
        )
    finally:
        conn.close()


def _unread_legacy_counts(conn: sqlite3.Connection, leftover: set[str]) -> list[str]:
    """Name each legacy table a modern read left untouched, with its row
    count, so a person deciding whether that matters does not have to open
    the database by hand to find out. `leftover` only ever holds names drawn
    from `LEGACY_TABLES`, a module constant - never a value read out of the
    database - so building the query with an f-string here carries none of
    the risk `sqltext.as_sql` exists to name."""
    lines = []
    for table in LEGACY_TABLES:
        if table not in leftover:
            continue
        (count,) = conn.execute(f"select count(*) from {table}").fetchone()
        lines.append(
            f"{table}: {count} row(s) not read - this database also has "
            f"{MODERN_TABLE!r}, and reading both risks double-importing rows "
            f"the migration to it already copied across"
        )
    return lines


def _open(path: Path) -> sqlite3.Connection:
    try:
        # `path` becomes part of a `file:` URI, where `?` and `#` are
        # syntax (query string, fragment) rather than literal path
        # characters - quote() escapes them so a path containing either
        # is not mis-parsed into the wrong file or a bad query param.
        uri = f"file:{quote(str(path))}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        # connect() is lazy, so a non-database file is only discovered on
        # the first read. Force it here, where the path is still in hand.
        conn.execute("select count(*) from sqlite_master")
    except sqlite3.Error as exc:
        raise UnreadableSource(f"{path} is not readable as sqlite: {exc}") from exc
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("select name from sqlite_master where type='table'")
    return {r[0] for r in rows}


def _read_modern(conn: sqlite3.Connection) -> ReadResult:
    """schema 33 and later: one table, `kind` tells rows apart.

    `kind='prompt'` rows are pulled out and grouped through `_prompts`
    before anything else touches them - the spec's "one entry per session,
    never one per prompt" rule is schema-neutral, and routing them through
    `_record` instead (as this function used to) would mint one near-empty
    entry per prompt, exactly what the spec rejects. Every other kind still
    goes through `_record` unchanged: `_body`'s narrative/text/facts/concepts
    rendering is already schema-neutral prose, and a `kind='summary'` row
    here has no `request`/`investigated`/`learned`/`completed`/`next_steps`
    columns to render as `_summary` does for the legacy shape - `_record`'s
    rendering is the only mapping that shape has to give.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "select m.*, p.name as project_name "
        "from memory_items m left join projects p on p.id = m.project_id "
        "order by m.created_at_epoch"
    )
    records: list[SourceRecord] = []
    skipped: list[str] = []
    prompt_rows: list[sqlite3.Row] = []
    for row in rows:
        if _column(row, "kind") == SourceKind.PROMPT.value:
            prompt_rows.append(row)
            continue
        record, skip = _record(MODERN_TABLE, row)
        if record is not None:
            records.append(record)
        if skip is not None:
            skipped.append(skip)
    records.extend(_prompts(prompt_rows))
    return ReadResult(records=records, skipped=skipped)


def _read_legacy(conn: sqlite3.Connection, tables: set[str]) -> ReadResult:
    """Pre-33 claude-mem: three tables, one per record kind.

    Each table is optional - a database that never recorded a prompt simply
    has no `user_prompts`, and refusing that would refuse a valid store.
    """
    conn.row_factory = sqlite3.Row
    records: list[SourceRecord] = []
    skipped: list[str] = []
    if "observations" in tables:
        rows = conn.execute("select * from observations order by created_at_epoch")
        for row in rows:
            record, skip = _record("observations", row)
            if record is not None:
                records.append(record)
            if skip is not None:
                skipped.append(skip)
    if "session_summaries" in tables:
        rows = conn.execute("select * from session_summaries order by created_at_epoch")
        records.extend(_summary(row) for row in rows)
    if "user_prompts" in tables:
        rows = conn.execute("select * from user_prompts order by id")
        records.extend(_prompts(list(rows)))
    return ReadResult(records=records, skipped=skipped)


def _column(row: sqlite3.Row, *names: str):
    """The first of `names` the row actually has. The modern reader joins
    `projects` and yields `project_name`; the legacy tables carry `project`
    inline. One accessor rather than two record builders."""
    available = row.keys()
    for name in names:
        if name in available:
            return row[name]
    return None


def _kind(raw_kind: str | None) -> SourceKind | None:
    """Resolve a row's `kind`, tolerating a value the enum does not know.

    This importer runs once, against a claude-mem version nobody has
    verified in advance. A `kind` string is that tool's private vocabulary,
    and a value added in some version between the fixtures this code was
    built against is plausible, not exceptional - the same reasoning
    `_json_list` already applies to one malformed column. Refusing the
    whole import over one unclassifiable row would throw away the other
    thousands of rows that read fine; returning `None` lets the caller
    drop just that row instead. A missing `kind` defaults to `OBSERVATION`
    rather than being treated as unknown - it is absent by schema, not
    malformed, and true of every row this function ever sees: `observations`
    is the only legacy table that reaches `_kind` at all (`session_summaries`
    and `user_prompts` build their `SourceKind` directly), and it has no
    `kind` column either.
    """
    if not raw_kind:
        return SourceKind.OBSERVATION
    try:
        return SourceKind(raw_kind)
    except ValueError:
        return None


def _record(table: str, row: sqlite3.Row) -> tuple[SourceRecord | None, str | None]:
    """Build one record, or say why the row could not become one.

    `table` names the source table purely for the skip message - the row
    itself carries no reliable way to say where it came from once it is a
    bare `sqlite3.Row`, and a skip line that cannot name its table is not
    reportable to a person deciding whether to go looking for it by hand.
    """
    raw_kind = _column(row, "kind")
    kind = _kind(raw_kind)
    if kind is None:
        return None, f"{table} {row['id']}: unknown kind {raw_kind!r}"
    return (
        SourceRecord(
            source_id=str(row["id"]),
            kind=kind,
            project=_column(row, "project_name", "project"),
            title=row["title"] or "(untitled)",
            summary=(row["subtitle"] or None),
            body=_body(row),
            tags=(f"{NAMESPACE}-type:{row['type']}",) if row["type"] else (),
            created_at=_when(row["created_at_epoch"]),
        ),
        None,
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


#: The five fields a legacy session summary carries, in the order they are
#: rendered. Spelled here rather than read off the row so that a column added
#: upstream cannot silently reorder a body.
SUMMARY_FIELDS: Final = (
    ("request", "Request"),
    ("investigated", "Investigated"),
    ("learned", "Learned"),
    ("completed", "Completed"),
    ("next_steps", "Next steps"),
)


def _summary(row: sqlite3.Row) -> SourceRecord:
    parts = []
    for field, heading in SUMMARY_FIELDS:
        value = (_column(row, field) or "").strip()
        if value:
            parts.append(f"## {heading}\n\n{value}")
    session = _column(row, "memory_session_id") or row["id"]
    return SourceRecord(
        source_id=f"summary:{row['id']}",
        kind=SourceKind.SUMMARY,
        project=_column(row, "project_name", "project"),
        title=f"Session summary {session}",
        summary=None,
        body="\n\n".join(parts),
        tags=(),
        created_at=_when(_column(row, "created_at_epoch")),
    )


def _prompts(rows: list[sqlite3.Row]) -> list[SourceRecord]:
    """One record per session, not per prompt.

    A single prompt is often a slash command - not knowledge, and one entry
    each would be dozens of near-empty entries competing in search with real
    memories. The ORDERED SEQUENCE of a session's prompts is the signal.

    Schema-neutral by construction, via `_column`'s name-fallback: the
    legacy `user_prompts` table has `content_session_id`/`prompt_text`, the
    modern `memory_items` table has `server_session_id`/`text` for a
    `kind='prompt'` row. The grouping rule is the same rule either way, so
    it lives here once rather than being re-derived per schema - the same
    reasoning `_column` already applies to `project_name`/`project`.
    """
    by_session: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        session = _column(row, "content_session_id", "server_session_id") or "unknown"
        by_session.setdefault(str(session), []).append(row)

    records = []
    for session, group in by_session.items():
        lines = []
        for n, row in enumerate(group, start=1):
            text = (_column(row, "prompt_text", "text") or "").strip()
            if text:
                lines.append(f"{n}. {text}")
        if not lines:
            continue
        records.append(
            SourceRecord(
                source_id=f"prompts:{session}",
                kind=SourceKind.PROMPT,
                project=None,
                title=f"Prompts from session {session}",
                summary=None,
                body="\n".join(lines),
                tags=(),
                created_at=_when(_column(group[0], "created_at_epoch")),
            )
        )
    return records


def _when(epoch: int | None) -> datetime | None:
    """claude-mem writes epoch milliseconds in some columns and seconds in
    others across versions. Values past the year 2200 are milliseconds."""
    if not epoch:
        return None
    seconds = epoch / 1000 if epoch > 7_258_118_400 else epoch
    return datetime.fromtimestamp(seconds, tz=timezone.utc)
