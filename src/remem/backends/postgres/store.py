"""The Postgres Store implementation. Hand-written SQL, psycopg 3, no ORM."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from remem.domain import (
    Collection,
    CollectionQuery,
    Entry,
    Event,
    EventKind,
    ExtractJob,
    HarnessStats,
    Hit,
    JobStatus,
    Kind,
    Match,
    Origin,
    Principal,
    PrincipalKind,
    Query,
    Scope,
    SessionRef,
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


def _vector_literal(vector: list[float]) -> str:
    """pgvector's text input format.

    Passed as a string and cast in SQL rather than adding the pgvector-python
    adapter: one more dependency for one type, when the literal form is
    stable, documented, and two lines. Revisit if vectors ever need reading
    back into Python, which no current caller does.
    """
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


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


EXTRACT_JOB_FIELDS = [
    "id", "owner_id", "project", "harness", "session_id", "covers_through",
    "status", "attempts", "error", "entries_written", "created_at",
    "updated_at",
]


def extract_job_columns(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{f}" for f in EXTRACT_JOB_FIELDS)


def _row_to_extract_job(row: dict) -> ExtractJob:
    return ExtractJob(
        id=row["id"],
        owner_id=row["owner_id"],
        project=row["project"],
        harness=row["harness"],
        session_id=row["session_id"],
        covers_through=row["covers_through"],
        status=JobStatus(row["status"]),
        attempts=row["attempts"],
        error=row["error"],
        entries_written=row["entries_written"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_session_ref(row: dict) -> SessionRef:
    return SessionRef(
        project=row["project"],
        harness=row["harness"],
        session_id=row["session_id"],
        event_count=row["event_count"],
        last_event_at=row["last_event_at"],
        extract_from=row["extract_from"],
    )


def _row_to_harness_stats(row: dict) -> HarnessStats:
    return HarnessStats(
        harness=row["harness"],
        events_24h=row["events_24h"],
        last_event_at=row["last_event_at"],
        sessions_awaiting=row["sessions_awaiting"],
    )


def _row_to_event(row: dict) -> Event:
    return Event(
        id=row["id"],
        owner_id=row["owner_id"],
        project=row["project"],
        harness=row["harness"],
        session_id=row["session_id"],
        kind=EventKind(row["kind"]),
        tool=row["tool"],
        payload=row["payload"],
        occurred_at=row["occurred_at"],
        recorded_at=row["recorded_at"],
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
            # Read the pre-write text so we can tell, after the upsert, whether
            # this write is the kind that invalidates a vector. A row that does
            # not exist yet counts as changed but has nothing to delete, so
            # text_changed stays True and the delete below is a no-op.
            cur.execute(
                "select title, body from entries where id = %s and owner_id = %s",
                (entry.id, entry.owner_id),
            )
            previous = cur.fetchone()
            text_changed = (
                previous is None
                or previous["title"] != entry.title
                or previous["body"] != entry.body
            )
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
            # Derived data must never outlive the text it was derived from.
            # An edited entry whose vector survives stays findable by its OLD
            # wording while returning its NEW body, and nothing downstream can
            # warn about it: the semantic tier's marker says "semantic", which
            # is true - the vector really is near the query. Deleting here
            # makes entries_missing_vectors offer the row again, so `remem
            # embed` repairs it on its next run.
            #
            # Only a text change counts. Linking, tagging and superseding all
            # go through put_entry too, and re-embedding on those would give
            # `remem embed` a backlog that never empties.
            if text_changed:
                cur.execute(
                    "delete from entry_vectors where entry_id = %s",
                    (entry.id,),
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

    def put_vector(
        self, entry_id: UUID, model: str, dim: int,
        vector: list[float], owner_id: UUID,
    ) -> None:
        """Insert or replace one entry's vector for one model.

        Ownership is checked here, inside the store, like every other write:
        a caller that could write vectors for another principal's entries
        would be an integrity hole no service check could close.
        """
        with self._cur() as cur:
            cur.execute(
                "select 1 from entries where id = %s and owner_id = %s",
                (entry_id, owner_id),
            )
            if cur.fetchone() is None:
                raise NotOwner(
                    f"entry {entry_id} does not belong to {owner_id}"
                )
            cur.execute(
                """
                insert into entry_vectors (entry_id, model, dim, vector)
                values (%(entry_id)s, %(model)s, %(dim)s, %(vector)s::vector)
                on conflict (entry_id, model) do update
                   set vector = excluded.vector,
                       dim = excluded.dim,
                       created_at = clock_timestamp()
                """,
                {"entry_id": entry_id, "model": model, "dim": dim,
                 "vector": _vector_literal(vector)},
            )

    def entries_missing_vectors(
        self, owner_id: UUID, model: str, limit: int
    ) -> list[Entry]:
        """Entries with no vector for this model, oldest first.

        Oldest first so a long backlog makes steady, resumable progress
        rather than re-visiting the same recent rows on every run.

        Superseded entries are skipped: they are invisible to every search
        tier, so embedding them is work whose result nothing can return.
        """
        with self._cur() as cur:
            cur.execute(
                f"""
                select {entry_columns("e")}
                from entries e
                left join entry_vectors v
                       on v.entry_id = e.id and v.model = %(model)s
                where e.owner_id = %(owner_id)s
                  and e.superseded_by is null
                  and v.entry_id is null
                order by e.created_at asc
                limit %(limit)s
                """,
                {"owner_id": owner_id, "model": model, "limit": limit},
            )
            return [_row_to_entry(r) for r in cur.fetchall()]

    def semantic_search(
        self, query: Query, owner_id: UUID, vector: list[float],
        model: str, threshold: float,
    ) -> list[Hit]:
        """Nearest neighbours by cosine similarity, above a floor.

        `<=>` is pgvector's cosine DISTANCE, so similarity is 1 - distance.
        Reported as similarity because that is the direction every other tier
        ranks in, and a mixed convention across tiers is how a comparison
        silently inverts.

        Exact search, no index - see 006_vectors.sql for why, and for when
        that stops being the right answer.

        The join is inner: an entry with no vector for this model is
        invisible here and reachable by the other two tiers. That is the
        correct degradation - an un-embedded entry is not lost, only less
        findable, and `remem embed` fixes it.
        """
        where, params = _entry_filters(query, owner_id)
        params["vector"] = _vector_literal(vector)
        params["model"] = model
        params["threshold"] = threshold

        similarity = "1 - (v.vector <=> %(vector)s::vector)"
        where.append("v.model = %(model)s")
        where.append(f"{similarity} >= %(threshold)s")

        sql = f"""
            select {entry_columns("e")},
                   {similarity} as rank,
                   left(e.body, 200) as snippet
            from entries e
            join entry_vectors v on v.entry_id = e.id
            where {" and ".join(where)}
            order by rank desc, e.created_at desc
            limit %(limit)s
        """
        with self._cur() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            Hit(entry=_row_to_entry(r), rank=float(r["rank"]),
                snippet=r["snippet"], match=Match.SEMANTIC)
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

    # ---------------- recording ----------------

    def set_record_enabled(
        self, owner_id: UUID, project: str, enabled: bool
    ) -> None:
        with self._cur() as cur:
            cur.execute(
                """
                insert into record_settings (owner_id, project, enabled)
                values (%s, %s, %s)
                on conflict (owner_id, project)
                  do update set enabled = excluded.enabled
                """,
                (owner_id, project, enabled),
            )

    def record_enabled(self, owner_id: UUID, project: str) -> bool:
        with self._cur() as cur:
            cur.execute(
                "select enabled from record_settings "
                "where owner_id = %s and project = %s",
                (owner_id, project),
            )
            row = cur.fetchone()
        return bool(row["enabled"]) if row else False

    def pending_legacy_capture_jobs(self, owner_id: UUID) -> int:
        """How many rows the retired capture spool still holds as pending.

        The only query left that touches `capture_jobs_legacy` (see
        010_retire_capture_jobs.sql). It exists so a user who opted into the
        old pipeline is told once that something is sitting there, rather
        than discovering a table nothing reads years later.
        """
        with self._cur() as cur:
            cur.execute(
                "select count(*) as n from capture_jobs_legacy "
                "where owner_id = %s and status = 'pending'",
                (owner_id,),
            )
            return int(cur.fetchone()["n"])

    def enabled_record_projects(self, owner_id: UUID) -> list[str]:
        """Projects with recording switched on, for the status commands."""
        with self._cur() as cur:
            cur.execute(
                "select project from record_settings "
                "where owner_id = %s and enabled order by project",
                (owner_id,),
            )
            return [r["project"] for r in cur.fetchall()]

    def event_stats(self, owner_id: UUID) -> list[HarnessStats]:
        """Per-harness recent volume for `remem record status`.

        Only harnesses with at least one recorded event ever appear in the
        result - there is no harness name to key a zero row on for one that
        has recorded nothing, which is exactly the failure this command
        exists to catch. `services.events.render` turns an empty list into a
        visible "no events" line rather than an absent section.

        `sessions_awaiting` is deliberately not computed here: that count
        depends on the "given up" rule, which is a policy decision that
        belongs in `services.extraction` (`_gave_up`/`awaiting_sessions`),
        not duplicated into SQL. `services.events.status` fills it in after
        this call by tallying `extraction.awaiting_sessions` per harness -
        see the comment there for why, and the task-8 review this answers.
        """
        with self._cur() as cur:
            cur.execute(
                """
                select harness,
                       count(*) filter (
                         where occurred_at
                                 >= clock_timestamp() - interval '24 hours'
                       ) as events_24h,
                       max(occurred_at) as last_event_at,
                       0 as sessions_awaiting
                  from events
                 where owner_id = %(owner_id)s
                 group by harness
                 order by harness
                """,
                {"owner_id": owner_id},
            )
            return [_row_to_harness_stats(r) for r in cur.fetchall()]

    # ---------------- events ----------------

    def put_event(self, event: Event) -> Event:
        """One INSERT. This is the hot path - it runs per tool call."""
        with self._cur() as cur:
            cur.execute(
                """
                insert into events (
                  id, owner_id, project, harness, session_id,
                  kind, tool, payload, occurred_at
                ) values (
                  %(id)s, %(owner_id)s, %(project)s, %(harness)s,
                  %(session_id)s, %(kind)s::event_kind, %(tool)s,
                  %(payload)s::jsonb, %(occurred_at)s
                )
                returning recorded_at
                """,
                {
                    "id": event.id,
                    "owner_id": event.owner_id,
                    "project": event.project,
                    "harness": event.harness,
                    "session_id": event.session_id,
                    "kind": str(event.kind),
                    "tool": event.tool,
                    "payload": json.dumps(event.payload),
                    # A NOT NULL column with no default is how a fail-soft hook
                    # turns into a lost event, so we default here rather than
                    # trust every caller to have set occurred_at.
                    "occurred_at": event.occurred_at or datetime.now(timezone.utc),
                },
            )
            event.recorded_at = cur.fetchone()["recorded_at"]
        return event

    def events_for_session(
        self,
        owner_id: UUID,
        project: str,
        harness: str,
        session_id: str,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[Event]:
        with self._cur() as cur:
            cur.execute(
                """
                select id, owner_id, project, harness, session_id, kind,
                       tool, payload, occurred_at, recorded_at
                  from events
                 where owner_id = %(owner_id)s and project = %(project)s
                   and harness = %(harness)s and session_id = %(session_id)s
                   and (%(since)s::timestamptz is null or occurred_at > %(since)s)
                 order by occurred_at
                 limit %(limit)s
                """,
                {
                    "owner_id": owner_id,
                    "project": project,
                    "harness": harness,
                    "session_id": session_id,
                    "since": since,
                    "limit": limit,
                },
            )
            return [_row_to_event(r) for r in cur.fetchall()]

    def delete_session_events(
        self, owner_id: UUID, project: str, harness: str, session_id: str
    ) -> int:
        """Delete every event for one session. Scoped by all four keys, not
        just owner_id and a time window - see the Protocol docstring for why
        `prune_events` is the wrong tool for this."""
        with self._cur() as cur:
            cur.execute(
                """
                delete from events
                 where owner_id = %(owner_id)s and project = %(project)s
                   and harness = %(harness)s and session_id = %(session_id)s
                """,
                {
                    "owner_id": owner_id,
                    "project": project,
                    "harness": harness,
                    "session_id": session_id,
                },
            )
            return cur.rowcount

    def link_entry_events(
        self, entry_id: UUID, events: list[Event], owner_id: UUID
    ) -> None:
        # Ownership is checked here, like every other write: the entry must
        # belong to this principal before we record anything about where it
        # came from.
        with self._cur() as cur:
            cur.execute(
                "select 1 from entries where id = %s and owner_id = %s",
                (entry_id, owner_id),
            )
            if cur.fetchone() is None:
                raise NotOwner(
                    f"entry {entry_id} does not belong to {owner_id}"
                )
            for event in events:
                cur.execute(
                    """
                    insert into entry_events (entry_id, event_id, session_id, harness)
                    values (%s, %s, %s, %s)
                    -- Re-running an extraction must not fail on provenance
                    -- it already wrote.
                    on conflict (entry_id, event_id) do nothing
                    """,
                    (entry_id, event.id, event.session_id, event.harness),
                )

    def provenance(
        self, entry_id: UUID, owner_id: UUID
    ) -> list[tuple[UUID, str, str, bool]]:
        """The forensic lookup, and the only query allowed to follow event_id.

        A left join, never an inner one: a pruned event must come back as a
        row with `present = False`, because "we recorded where this came
        from and then deleted the raw" and "we never recorded anything" are
        different answers and the user needs to be able to tell them apart.
        """
        with self._cur() as cur:
            cur.execute(
                """
                select ee.event_id, ee.session_id, ee.harness,
                       (ev.id is not null) as present
                  from entry_events ee
                  join entries e on e.id = ee.entry_id
             left join events ev on ev.id = ee.event_id
                 where ee.entry_id = %s and e.owner_id = %s
                 order by ee.event_id
                """,
                (entry_id, owner_id),
            )
            return [
                (r["event_id"], r["session_id"], r["harness"], r["present"])
                for r in cur.fetchall()
            ]

    def prune_events(
        self, owner_id: UUID, before: datetime, force: bool
    ) -> tuple[int, int, int]:
        """Delete raw events older than `before`, and say what that cost.

        "Extracted" uses the same watermark rule as
        `sessions_awaiting_extraction` - the newest `covers_through` among a
        session's DONE jobs - so prune and process can never disagree about
        what has already been extracted. `--force` (the `force` argument)
        drops that condition entirely rather than widening it: an unextracted
        event is raw that produced nothing, and losing it is the outcome the
        whole pipeline exists to prevent, so overriding that is a deliberate
        act, not a wider window.

        The dangling count is taken from `entry_events` before the delete
        runs, in the same statement - after the delete the rows are already
        dangling and counting them then would just be re-deriving what this
        statement did. `kept_unextracted` is the other side of the refusal:
        how many events in the window survived only because they had not
        been extracted yet.
        """
        with self._cur() as cur:
            cur.execute(
                """
                with watermarks as (
                  select project, harness, session_id, max(covers_through) as mark
                    from extract_jobs
                   where owner_id = %(owner_id)s and status = 'done'
                   group by project, harness, session_id
                ), scoped as (
                  select e.id,
                         (w.mark is not null and e.occurred_at <= w.mark)
                           as extracted
                    from events e
                    left join watermarks w
                           on w.project = e.project and w.harness = e.harness
                          and w.session_id = e.session_id
                   where e.owner_id = %(owner_id)s
                     and e.occurred_at < %(before)s
                ), candidates as (
                  select id from scoped where %(force)s or extracted
                ), dangling as (
                  select count(*) as n from entry_events
                   where event_id in (select id from candidates)
                ), deleted as (
                  delete from events where id in (select id from candidates)
                  returning id
                )
                select (select count(*) from deleted) as deleted,
                       (select n from dangling) as dangling,
                       -- Under --force nothing is "kept" for lack of
                       -- extraction - it was deleted along with everything
                       -- else in the window, so this is unconditionally 0
                       -- rather than a count of rows that no longer exist.
                       (case when %(force)s then 0
                             else (select count(*) from scoped
                                    where not extracted) end)
                         as kept_unextracted
                """,
                {"owner_id": owner_id, "before": before, "force": force},
            )
            row = cur.fetchone()
            return (row["deleted"], row["dangling"], row["kept_unextracted"])

    # ---------------- extraction spool ----------------

    def get_extract_job(self, job_id: UUID, owner_id: UUID) -> ExtractJob | None:
        with self._cur() as cur:
            cur.execute(
                f"select {extract_job_columns()} from extract_jobs "
                "where id = %s and owner_id = %s",
                (job_id, owner_id),
            )
            row = cur.fetchone()
        return _row_to_extract_job(row) if row else None

    def extract_job_counts(self, owner_id: UUID) -> dict[str, int]:
        with self._cur() as cur:
            cur.execute(
                "select status, count(*) as n from extract_jobs "
                "where owner_id = %s group by status",
                (owner_id,),
            )
            return {str(r["status"]): r["n"] for r in cur.fetchall()}

    def recent_failed_extract_jobs(
        self, owner_id: UUID, limit: int = 5
    ) -> list[ExtractJob]:
        with self._cur() as cur:
            cur.execute(
                f"""
                select {extract_job_columns()} from extract_jobs
                 where owner_id = %s and status = 'failed'
                 order by updated_at desc
                 limit %s
                """,
                (owner_id, limit),
            )
            return [_row_to_extract_job(r) for r in cur.fetchall()]

    def finish_extract_job(
        self,
        job_id: UUID,
        owner_id: UUID,
        status: JobStatus,
        error: str | None,
        entries_written: int,
        covers_through: datetime | None,
    ) -> None:
        with self._cur() as cur:
            cur.execute(
                """
                update extract_jobs
                   set status = %s, error = %s, entries_written = %s,
                       covers_through = %s, updated_at = clock_timestamp()
                 where id = %s and owner_id = %s
                """,
                (str(status), error, entries_written, covers_through,
                 job_id, owner_id),
            )

    def try_advisory_lock(self, name: str, owner_id: UUID) -> bool:
        # Session-level, not transaction-level: `events process` runs with
        # autocommit on, so a transaction-scoped lock would be released at
        # the first commit - which is the first job it finishes, exactly
        # when a second run must still be kept out. The lock dies with the
        # connection, which is the process ending, which is what we want.
        #
        # Postgres's advisory lock functions come in a one-bigint and a
        # two-int form; the two-int form is used here so the command name
        # and the owner can be hashed independently instead of combined into
        # one 64-bit value, which would need care to avoid collisions between
        # different (name, owner) pairs landing on the same bigint.
        with self._cur() as cur:
            cur.execute(
                "select pg_try_advisory_lock(hashtext(%s), hashtext(%s))",
                (name, str(owner_id)),
            )
            return bool(cur.fetchone()["pg_try_advisory_lock"])

    def extract_job_for_session(
        self, owner_id: UUID, session: SessionRef
    ) -> ExtractJob | None:
        """The job this session already has, if any. Reads nothing else.

        Deliberately status-blind: what to do with a job that has given up
        is a policy question, and policy lives in services/. This just says
        what the row is.
        """
        with self._cur() as cur:
            cur.execute(
                f"""
                select {extract_job_columns()} from extract_jobs
                 where owner_id = %s and project = %s and harness = %s
                   and session_id = %s
                """,
                (owner_id, session.project, session.harness,
                 session.session_id),
            )
            row = cur.fetchone()
        return _row_to_extract_job(row) if row else None

    def claim_extract_job(self, owner_id: UUID, session: SessionRef) -> ExtractJob:
        """Upsert on the session key, so a retried session reuses its row
        and its attempt count instead of accumulating one row per attempt."""
        with self._cur() as cur:
            cur.execute(
                f"""
                insert into extract_jobs (
                  id, owner_id, project, harness, session_id, status, attempts
                ) values (%(id)s, %(owner_id)s, %(project)s, %(harness)s,
                          %(session_id)s, 'running', 1)
                on conflict (owner_id, project, harness, session_id)
                  do update set status = 'running',
                                attempts = extract_jobs.attempts + 1,
                                updated_at = clock_timestamp()
                returning {extract_job_columns()}
                """,
                {
                    "id": new_id(),
                    "owner_id": owner_id,
                    "project": session.project,
                    "harness": session.harness,
                    "session_id": session.session_id,
                },
            )
            return _row_to_extract_job(cur.fetchone())

    def claim_extract_job_by_id(
        self, job_id: UUID, owner_id: UUID
    ) -> ExtractJob | None:
        """Claim one named job whatever its status, for `process --job ID`.

        Unlike claim_extract_job this ignores status entirely: retrying a
        job that already gave up is the whole point of the flag. SKIP LOCKED
        still keeps a concurrent run from taking the same row.
        """
        with self._cur() as cur:
            cur.execute(
                f"""
                with claimed as (
                  select id from extract_jobs
                   where id = %(id)s and owner_id = %(owner_id)s
                   for update skip locked
                )
                update extract_jobs j
                   set status = 'running',
                       attempts = j.attempts + 1,
                       updated_at = clock_timestamp()
                  from claimed
                 where j.id = claimed.id
                returning {extract_job_columns("j")}
                """,
                {"id": job_id, "owner_id": owner_id},
            )
            row = cur.fetchone()
        return _row_to_extract_job(row) if row else None

    def sessions_awaiting_extraction(
        self, owner_id: UUID, idle_seconds: int, limit: int
    ) -> list[SessionRef]:
        """Sessions with events past their watermark, quiet long enough.

        The `watermarks` CTE gives the newest covers_through per session
        among its DONE jobs - the newest, not any, because a session can be
        extracted more than once across its life and only the latest
        watermark matters. Events at or before that mark already produced
        whatever they were going to produce; event_count and the idle check
        both look only at what is left after it, which is what makes
        event_count mean "work outstanding" rather than "events that exist".
        """
        with self._cur() as cur:
            cur.execute(
                """
                with watermarks as (
                  select project, harness, session_id, max(covers_through) as mark
                    from extract_jobs
                   where owner_id = %(owner_id)s and status = 'done'
                   group by project, harness, session_id
                )
                select e.project, e.harness, e.session_id,
                       count(*) as event_count,
                       max(e.occurred_at) as last_event_at,
                       w.mark as extract_from
                  from events e
                  left join watermarks w
                         on w.project = e.project
                        and w.harness = e.harness
                        and w.session_id = e.session_id
                 where e.owner_id = %(owner_id)s
                   and (w.mark is null or e.occurred_at > w.mark)
                 group by e.project, e.harness, e.session_id, w.mark
                having max(e.occurred_at)
                         < clock_timestamp() - make_interval(secs => %(idle)s)
                 order by max(e.occurred_at)
                 limit %(limit)s
                """,
                {"owner_id": owner_id, "idle": idle_seconds, "limit": limit},
            )
            return [_row_to_session_ref(r) for r in cur.fetchall()]
