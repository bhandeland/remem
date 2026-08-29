"""The Postgres Store implementation. Hand-written SQL, psycopg 3, no ORM."""

from __future__ import annotations

import json
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from remem.domain import (
    CaptureJob,
    CaptureStatus,
    Collection,
    CollectionQuery,
    Entry,
    Hit,
    Kind,
    Match,
    Origin,
    Principal,
    PrincipalKind,
    Query,
    Scope,
    new_id,
)
from remem.store import NotOwner

ENTRY_FIELDS = [
    "id", "kind", "title", "body", "project", "scope", "owner_id", "tags",
    "links", "agent", "session_id", "origin", "superseded_by",
    "created_at", "updated_at",
]


def entry_columns(alias: str = "") -> str:
    """Column list, optionally table-qualified for joins."""
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{f}" for f in ENTRY_FIELDS)


def _entry_filters(query: Query, owner_id: UUID) -> tuple[list[str], dict]:
    """The filters every entry read applies, built once for all search tiers.

    Extracted because there are now three tiers running the same predicates
    against the same table. Duplicated, they drift: a filter accidentally
    dropped from one tier is invisible until that tier happens to answer, and
    the owner check is among them. One builder means one place to be wrong.

    Returns clauses joined by the caller with " and ", plus the params they
    reference. Text matching is NOT included - that is what differs between
    tiers and is the caller's business.
    """
    params: dict = {"owner_id": owner_id, "limit": query.limit}
    where = ["e.owner_id = %(owner_id)s"]

    if not query.include_superseded:
        where.append("e.superseded_by is null")
    if query.kinds:
        where.append("e.kind = any(%(kinds)s::entry_kind[])")
        params["kinds"] = [str(k) for k in query.kinds]
    if query.project is not None:
        where.append("e.project = %(project)s")
        params["project"] = query.project
    if query.tags:
        where.append("e.tags && %(tags)s")
        params["tags"] = list(query.tags)
    if query.origins:
        where.append("e.origin = any(%(origins)s::entry_origin[])")
        params["origins"] = [str(o) for o in query.origins]
    if query.since is not None:
        where.append("e.created_at >= %(since)s")
        params["since"] = query.since

    return where, params


def _row_to_entry(row: dict) -> Entry:
    return Entry(
        id=row["id"],
        kind=Kind(row["kind"]),
        title=row["title"],
        body=row["body"],
        owner_id=row["owner_id"],
        project=row["project"],
        scope=Scope(row["scope"]),
        tags=list(row["tags"] or []),
        links=[UUID(str(x)) for x in (row["links"] or [])],
        agent=row["agent"],
        session_id=row["session_id"],
        origin=Origin(row["origin"]),
        superseded_by=row["superseded_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_collection(row: dict) -> Collection:
    query = row["query"]
    if isinstance(query, str):
        query = json.loads(query)
    return Collection(
        id=row["id"],
        slug=row["slug"],
        title=row["title"],
        owner_id=row["owner_id"],
        description=row["description"],
        project=row["project"],
        scope=Scope(row["scope"]),
        query=CollectionQuery.from_dict(query),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


CAPTURE_JOB_FIELDS = [
    "id", "owner_id", "project", "session_id", "transcript_path", "status",
    "attempts", "error", "entries_written", "created_at", "updated_at",
]


def capture_job_columns(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{f}" for f in CAPTURE_JOB_FIELDS)


def _row_to_capture_job(row: dict) -> CaptureJob:
    return CaptureJob(
        id=row["id"],
        owner_id=row["owner_id"],
        project=row["project"],
        session_id=row["session_id"],
        transcript_path=row["transcript_path"],
        status=CaptureStatus(row["status"]),
        attempts=row["attempts"],
        error=row["error"],
        entries_written=row["entries_written"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PostgresStore:
    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def _cur(self):
        return self._conn.cursor(row_factory=dict_row)

    # ---------------- principals ----------------

    def ensure_principal(self, handle: str) -> Principal:
        existing = self.get_principal(handle)
        if existing is not None:
            return existing
        with self._cur() as cur:
            cur.execute(
                """
                insert into principals (id, handle) values (%s, %s)
                on conflict (handle) do nothing
                """,
                (new_id(), handle),
            )
        # A concurrent caller may have won the race to create this handle;
        # re-select rather than trust the insert to have landed our row.
        created = self.get_principal(handle)
        assert created is not None
        return created

    def get_principal(self, handle: str) -> Principal | None:
        with self._cur() as cur:
            cur.execute(
                "select id, handle, display_name, kind, created_at "
                "from principals where handle = %s",
                (handle,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Principal(
            id=row["id"],
            handle=row["handle"],
            display_name=row["display_name"],
            kind=PrincipalKind(row["kind"]),
            created_at=row["created_at"],
        )

    # ---------------- entries ----------------

    def put_entry(self, entry: Entry) -> Entry:
        with self._cur() as cur:
            cur.execute(
                f"""
                insert into entries (
                  id, kind, title, body, project, scope, owner_id, tags, links,
                  agent, session_id, origin, superseded_by
                ) values (
                  %(id)s, %(kind)s, %(title)s, %(body)s, %(project)s, %(scope)s,
                  %(owner_id)s, %(tags)s, %(links)s, %(agent)s, %(session_id)s,
                  %(origin)s, %(superseded_by)s
                )
                on conflict (id) do update set
                  kind = excluded.kind, title = excluded.title,
                  body = excluded.body, project = excluded.project,
                  scope = excluded.scope, tags = excluded.tags,
                  links = excluded.links, agent = excluded.agent,
                  session_id = excluded.session_id, origin = excluded.origin,
                  superseded_by = excluded.superseded_by,
                  updated_at = clock_timestamp()
                where entries.owner_id = %(owner_id)s
                returning {entry_columns()}
                """,
                {
                    "id": entry.id,
                    "kind": str(entry.kind),
                    "title": entry.title,
                    "body": entry.body,
                    "project": entry.project,
                    "scope": str(entry.scope),
                    "owner_id": entry.owner_id,
                    "tags": list(entry.tags),
                    "links": [str(x) for x in entry.links],
                    "agent": entry.agent,
                    "session_id": entry.session_id,
                    "origin": str(entry.origin),
                    "superseded_by": entry.superseded_by,
                },
            )
            row = cur.fetchone()
        if row is None:
            # The id exists but belongs to someone else, so the ON CONFLICT
            # update matched no row. Never silently drop the write.
            raise NotOwner(
                f"entry {entry.id} exists and is owned by another principal"
            )
        return _row_to_entry(row)

    def get_entry(self, entry_id: UUID, owner_id: UUID) -> Entry | None:
        with self._cur() as cur:
            cur.execute(
                f"select {entry_columns()} from entries "
                "where id = %s and owner_id = %s",
                (entry_id, owner_id),
            )
            row = cur.fetchone()
        return _row_to_entry(row) if row else None

    def set_superseded(self, old_id: UUID, new_entry_id: UUID, owner_id: UUID) -> bool:
        with self._cur() as cur:
            cur.execute(
                """
                update entries
                   set superseded_by = %(new_entry_id)s, updated_at = clock_timestamp()
                 where id = %(old_id)s
                   and owner_id = %(owner_id)s
                   and exists (
                         select 1 from entries
                          where id = %(new_entry_id)s and owner_id = %(owner_id)s
                       )
                """,
                {"new_entry_id": new_entry_id, "old_id": old_id, "owner_id": owner_id},
            )
            return cur.rowcount == 1

    def search(self, query: Query, owner_id: UUID) -> list[Hit]:
        """Ranked search. Owner and superseded filters are always applied."""
        text = (query.text or "").strip()
        where, params = _entry_filters(query, owner_id)

        if text:
            # websearch_to_tsquery accepts what people and agents actually
            # type, and never raises a syntax error on odd input.
            params["text"] = text
            where.append("e.search @@ q")
            # Empty StartSel/StopSel: the snippet feeds --json output and
            # agent context, where ts_headline's default <b> tags are markup
            # nobody renders and tokens everybody pays for.
            sql = f"""
                select {entry_columns("e")},
                       ts_rank_cd(e.search, q) as rank,
                       ts_headline('english', e.body, q,
                                   'MaxWords=32,MinWords=8,ShortWord=2,'
                                   'StartSel="",StopSel=""') as snippet
                from entries e,
                     websearch_to_tsquery('english', %(text)s) q
                where {" and ".join(where)}
                order by rank desc, e.created_at desc
                limit %(limit)s
            """
        else:
            sql = f"""
                select {entry_columns("e")},
                       0::float4 as rank,
                       left(e.body, 200) as snippet
                from entries e
                where {" and ".join(where)}
                order by e.created_at desc
                limit %(limit)s
            """

        with self._cur() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            Hit(entry=_row_to_entry(r), rank=float(r["rank"]),
                snippet=r["snippet"], match=Match.EXACT)
            for r in rows
        ]

    def fuzzy_search(
        self, query: Query, owner_id: UUID, threshold: float
    ) -> list[Hit]:
        """Typo-tolerant search, for when exact search found nothing.

        Two operators, because they behave differently on the two columns:
        `similarity` compares whole strings, which works on a short title but
        dissolves into noise on a long body; `word_similarity` compares the
        query against the best-matching word sequence, which is what makes a
        misspelled word inside a body findable. The score is the better of the
        two, so an entry can match on either.
        """
        text = (query.text or "").strip()
        if not text:
            return []

        where, params = _entry_filters(query, owner_id)
        params["text"] = text
        params["threshold"] = threshold

        score = ("greatest(similarity(e.title, %(text)s), "
                 "word_similarity(%(text)s, e.body))")
        where.append(f"{score} >= %(threshold)s")

        sql = f"""
            select {entry_columns("e")},
                   {score} as rank,
                   left(e.body, 200) as snippet
            from entries e
            where {" and ".join(where)}
            order by rank desc, e.created_at desc
            limit %(limit)s
        """
        with self._cur() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            Hit(
                entry=_row_to_entry(r),
                rank=float(r["rank"]),
                snippet=r["snippet"],
                match=Match.FUZZY,
            )
            for r in rows
        ]

    # ---------------- collections ----------------

    def put_collection(self, collection: Collection) -> Collection:
        with self._cur() as cur:
            cur.execute(
                """
                insert into collections (
                  id, slug, title, description, project, scope, owner_id, query
                ) values (
                  %(id)s, %(slug)s, %(title)s, %(description)s, %(project)s,
                  %(scope)s, %(owner_id)s, %(query)s
                )
                on conflict (owner_id, slug) do update set
                  title = excluded.title, description = excluded.description,
                  project = excluded.project, scope = excluded.scope,
                  query = excluded.query, updated_at = now()
                returning id, slug, title, description, project, scope,
                          owner_id, query, created_at, updated_at
                """,
                {
                    "id": collection.id,
                    "slug": collection.slug,
                    "title": collection.title,
                    "description": collection.description,
                    "project": collection.project,
                    "scope": str(collection.scope),
                    "owner_id": collection.owner_id,
                    "query": json.dumps(collection.query.to_dict()),
                },
            )
            return _row_to_collection(cur.fetchone())

    def get_collection(self, slug: str, owner_id: UUID) -> Collection | None:
        with self._cur() as cur:
            cur.execute(
                "select id, slug, title, description, project, scope, owner_id,"
                " query, created_at, updated_at from collections "
                "where slug = %s and owner_id = %s",
                (slug, owner_id),
            )
            row = cur.fetchone()
        return _row_to_collection(row) if row else None

    def list_collections(self, owner_id: UUID) -> list[Collection]:
        with self._cur() as cur:
            cur.execute(
                "select id, slug, title, description, project, scope, owner_id,"
                " query, created_at, updated_at from collections "
                "where owner_id = %s order by slug",
                (owner_id,),
            )
            return [_row_to_collection(r) for r in cur.fetchall()]

    def pin(
        self, collection_id: UUID, entry_id: UUID, position: int, owner_id: UUID
    ) -> bool:
        """Pin an entry into a collection. Both must belong to owner_id.

        Returns False when the ownership guards match nothing, rather than
        writing nothing silently — the same contract as set_superseded.
        """
        with self._cur() as cur:
            cur.execute(
                """
                insert into collection_members (collection_id, entry_id, position)
                select %(collection_id)s, %(entry_id)s, %(position)s
                 where exists (select 1 from collections
                                where id = %(collection_id)s
                                  and owner_id = %(owner_id)s)
                   and exists (select 1 from entries
                                where id = %(entry_id)s
                                  and owner_id = %(owner_id)s)
                on conflict (collection_id, entry_id)
                  do update set position = excluded.position
                """,
                {
                    "collection_id": collection_id,
                    "entry_id": entry_id,
                    "position": position,
                    "owner_id": owner_id,
                },
            )
            return cur.rowcount == 1

    def pinned_entries(self, collection_id: UUID, owner_id: UUID) -> list[Entry]:
        with self._cur() as cur:
            cur.execute(
                f"""
                select {entry_columns("e")}
                from collection_members m
                join entries e on e.id = m.entry_id
                where m.collection_id = %s and e.owner_id = %s
                order by m.position, e.created_at
                """,
                (collection_id, owner_id),
            )
            return [_row_to_entry(r) for r in cur.fetchall()]

    # ---------------- capture ----------------

    def set_capture_enabled(
        self, owner_id: UUID, project: str, enabled: bool
    ) -> None:
        with self._cur() as cur:
            cur.execute(
                """
                insert into capture_settings (owner_id, project, enabled)
                values (%s, %s, %s)
                on conflict (owner_id, project)
                  do update set enabled = excluded.enabled
                """,
                (owner_id, project, enabled),
            )

    def capture_enabled(self, owner_id: UUID, project: str) -> bool:
        with self._cur() as cur:
            cur.execute(
                "select enabled from capture_settings "
                "where owner_id = %s and project = %s",
                (owner_id, project),
            )
            row = cur.fetchone()
        return bool(row["enabled"]) if row else False

    def enqueue_capture(self, job: CaptureJob) -> CaptureJob:
        with self._cur() as cur:
            cur.execute(
                f"""
                insert into capture_jobs (
                  id, owner_id, project, session_id, transcript_path
                ) values (%s, %s, %s, %s, %s)
                returning {capture_job_columns()}
                """,
                (job.id, job.owner_id, job.project, job.session_id,
                 job.transcript_path),
            )
            return _row_to_capture_job(cur.fetchone())

    def claim_capture_jobs(
        self, owner_id: UUID, limit: int, stale_after_seconds: int = 600
    ) -> list[CaptureJob]:
        """Claim pending jobs, plus any left running by a dead drain.

        SKIP LOCKED is what makes a second concurrent drain safe without a
        broker: each transaction takes rows the other has not locked.
        """
        with self._cur() as cur:
            cur.execute(
                f"""
                with claimed as (
                  select id from capture_jobs
                   where owner_id = %(owner_id)s
                     and (
                           status = 'pending'
                        or (status = 'running'
                            and updated_at <
                                clock_timestamp()
                                - make_interval(secs => %(stale)s))
                         )
                   order by created_at
                   limit %(limit)s
                   for update skip locked
                )
                update capture_jobs j
                   set status = 'running',
                       attempts = j.attempts + 1,
                       updated_at = clock_timestamp()
                  from claimed
                 where j.id = claimed.id
                returning {capture_job_columns("j")}
                """,
                {"owner_id": owner_id, "limit": limit,
                 "stale": stale_after_seconds},
            )
            return [_row_to_capture_job(r) for r in cur.fetchall()]

    def claim_capture_job(
        self, job_id: UUID, owner_id: UUID
    ) -> CaptureJob | None:
        """Claim one named job whatever its status, for `drain --job ID`.

        Unlike claim_capture_jobs this ignores status entirely: retrying a
        job that already gave up is the whole point of the flag. SKIP LOCKED
        still keeps a concurrent drain from taking the same row.
        """
        with self._cur() as cur:
            cur.execute(
                f"""
                with claimed as (
                  select id from capture_jobs
                   where id = %(id)s and owner_id = %(owner_id)s
                   for update skip locked
                )
                update capture_jobs j
                   set status = 'running',
                       attempts = j.attempts + 1,
                       updated_at = clock_timestamp()
                  from claimed
                 where j.id = claimed.id
                returning {capture_job_columns("j")}
                """,
                {"id": job_id, "owner_id": owner_id},
            )
            row = cur.fetchone()
        return _row_to_capture_job(row) if row else None

    def finish_capture_job(
        self,
        job_id: UUID,
        owner_id: UUID,
        status: CaptureStatus,
        error: str | None,
        entries_written: int,
    ) -> None:
        with self._cur() as cur:
            cur.execute(
                """
                update capture_jobs
                   set status = %s, error = %s, entries_written = %s,
                       updated_at = clock_timestamp()
                 where id = %s and owner_id = %s
                """,
                (str(status), error, entries_written, job_id, owner_id),
            )

    def get_capture_job(self, job_id: UUID, owner_id: UUID) -> CaptureJob | None:
        with self._cur() as cur:
            cur.execute(
                f"select {capture_job_columns()} from capture_jobs "
                "where id = %s and owner_id = %s",
                (job_id, owner_id),
            )
            row = cur.fetchone()
        return _row_to_capture_job(row) if row else None

    def capture_job_counts(self, owner_id: UUID) -> dict[str, int]:
        with self._cur() as cur:
            cur.execute(
                "select status, count(*) as n from capture_jobs "
                "where owner_id = %s group by status",
                (owner_id,),
            )
            return {str(r["status"]): r["n"] for r in cur.fetchall()}

    def recent_failed_capture_jobs(
        self, owner_id: UUID, limit: int = 5
    ) -> list[CaptureJob]:
        with self._cur() as cur:
            cur.execute(
                f"""
                select {capture_job_columns()} from capture_jobs
                 where owner_id = %s and status = 'failed'
                 order by updated_at desc
                 limit %s
                """,
                (owner_id, limit),
            )
            return [_row_to_capture_job(r) for r in cur.fetchall()]

    def enabled_capture_projects(self, owner_id: UUID) -> list[str]:
        """Projects with capture switched on, for `remem capture status`."""
        with self._cur() as cur:
            cur.execute(
                "select project from capture_settings "
                "where owner_id = %s and enabled order by project",
                (owner_id,),
            )
            return [r["project"] for r in cur.fetchall()]
