"""The Postgres Store implementation. Hand-written SQL, psycopg 3, no ORM."""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from remem.domain import (
    Collection,
    CollectionQuery,
    DuplicateGroup,
    DuplicateSet,
    Entry,
    Event,
    EventKind,
    ExtractJob,
    HarnessStats,
    Hit,
    IngestDesignation,
    IngestRun,
    IngestTrigger,
    JobStatus,
    Kind,
    Match,
    MemoryDesignation,
    MemoryRun,
    MemoryTrigger,
    NearPair,
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
    "id", "kind", "title", "body", "summary", "project", "scope", "owner_id",
    "tags", "links", "agent", "session_id", "origin", "superseded_by",
    "created_at", "updated_at",
]


def entry_columns(alias: str = "") -> str:
    """Column list, optionally table-qualified for joins."""
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{f}" for f in ENTRY_FIELDS)


def _aliased_entry_columns(alias: str, prefix: str) -> str:
    """Entry columns renamed, for a query joining `entries` to itself.

    Two copies of the same table produce two `id` keys in one result row,
    and the second silently wins. Prefixing makes both readable.
    """
    return ", ".join(f"{alias}.{f} as {prefix}{f}" for f in ENTRY_FIELDS)


def _row_to_entry_prefixed(row: dict, prefix: str) -> Entry:
    return _row_to_entry(
        {k[len(prefix):]: v for k, v in row.items() if k.startswith(prefix)}
    )


MEMORY_RUN_FIELDS = [
    "id", "owner_id", "project", "trigger", "started_at", "finished_at",
    "adopted", "healed", "edited", "regenerated", "deleted", "unchanged",
    "renamed", "conflicts", "sidecars", "failures",
]


def memory_run_columns() -> str:
    return ", ".join(MEMORY_RUN_FIELDS)


def _row_to_memory_run(row: dict) -> MemoryRun:
    return MemoryRun(
        id=row["id"],
        owner_id=row["owner_id"],
        project=row["project"],
        trigger=MemoryTrigger(row["trigger"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        adopted=row["adopted"],
        healed=row["healed"],
        edited=row["edited"],
        regenerated=row["regenerated"],
        deleted=row["deleted"],
        unchanged=row["unchanged"],
        renamed=list(row["renamed"] or []),
        conflicts=list(row["conflicts"] or []),
        sidecars=list(row["sidecars"] or []),
        failures=list(row["failures"] or []),
    )


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
        summary=row["summary"],
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


INGEST_RUN_FIELDS = [
    "id", "owner_id", "project", "trigger", "archive", "started_at",
    "finished_at", "created", "changed", "unchanged", "swept", "embedded",
    "failures", "twins", "embed_error",
]


def ingest_run_columns(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{f}" for f in INGEST_RUN_FIELDS)


def _row_to_ingest_run(row: dict) -> IngestRun:
    return IngestRun(
        id=row["id"],
        owner_id=row["owner_id"],
        project=row["project"],
        trigger=IngestTrigger(row["trigger"]),
        archive=row["archive"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        created=row["created"],
        changed=row["changed"],
        unchanged=row["unchanged"],
        swept=row["swept"],
        embedded=row["embedded"],
        failures=list(row["failures"]),
        twins=list(row["twins"]),
        embed_error=row["embed_error"],
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
                  id, kind, title, body, summary, project, scope, owner_id,
                  tags, links, agent, session_id, origin, superseded_by
                ) values (
                  %(id)s, %(kind)s, %(title)s, %(body)s, %(summary)s,
                  %(project)s, %(scope)s, %(owner_id)s, %(tags)s, %(links)s,
                  %(agent)s, %(session_id)s, %(origin)s, %(superseded_by)s
                )
                on conflict (id) do update set
                  kind = excluded.kind, title = excluded.title,
                  body = excluded.body, summary = excluded.summary,
                  project = excluded.project,
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
                    "summary": entry.summary,
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

    def exact_duplicate_groups(
        self, query: Query, owner_id: UUID
    ) -> list[DuplicateSet]:
        """Live entries sharing a body, grouped.

        The checksum is over the body ALONE and trimmed. Body alone because
        two entries holding one fact under different titles are duplicates,
        and the extractor's title is never the one a human would have
        chosen. This is deliberately the opposite of `ingest`, which
        compares title and body - that comparison asks whether a chunk needs
        re-indexing, and there the title is half the embedding text.

        `btrim` because a hand-written memory file and a remem-generated one
        can differ by a trailing newline, and that is not a different fact.
        The character set is spelled out: one-argument `btrim` strips spaces
        ONLY, so the newline case - the one this exists for - would have
        sailed straight past it.

        Nothing looser: normalising interior whitespace would start merging
        entries whose formatting genuinely differs, which is the near tier's
        job, with a score attached.

        A window function rather than a `group by` subquery so that
        `_entry_filters` is applied exactly once - a second copy under a
        second alias is how a filter silently drifts out of one path.

        No `limit`. The groups are a finite, cheap fact about the store, and
        a truncated list of identical bodies would hide the easiest half of
        this report's own answer.
        """
        where, params = _entry_filters(query, owner_id)
        sql = f"""
            select * from (
                select {entry_columns("e")},
                       md5(btrim(e.body, E' \\t\\n\\r')) as body_key,
                       count(*) over (partition by md5(btrim(e.body, E' \\t\\n\\r'))) as n
                from entries e
                where {" and ".join(where)}
            ) s
            where s.n > 1
            order by s.body_key, s.created_at asc
        """
        with self._cur() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        groups: dict[str, list[Entry]] = {}
        for r in rows:
            groups.setdefault(r["body_key"], []).append(_row_to_entry(r))
        return [DuplicateSet(entries=members) for members in groups.values()]

    def near_duplicate_pairs(
        self, query: Query, owner_id: UUID, model: str,
        threshold: float, limit: int,
    ) -> tuple[list[NearPair], int]:
        """Entry pairs above a cosine-similarity floor, and how many there are.

        `b.id > a.id` so each pair is computed and reported once rather than
        twice in both orders.

        The join is inner on both sides, so an entry with no vector for this
        model is invisible here - the same correct degradation
        `semantic_search` documents. `vector_coverage` is what tells the
        caller how much of the population that silently excluded.

        The count is taken over the whole matching set, not the returned
        page, because a report that truncates without saying so is the
        diagnostic that eventually lies confidently.

        Exact, no index, like `semantic_search` - see 006_vectors.sql.
        """
        where, params = _entry_filters(query, owner_id)
        # _entry_filters writes its clauses against the alias `e`. This query
        # has two entry aliases, so the same predicate is applied to both -
        # rebuilt by substitution rather than by a second hand-written copy,
        # which is how a filter drifts out of one side unnoticed.
        a_where = [c.replace("e.", "a.") for c in where]
        b_where = [c.replace("e.", "b.") for c in where]
        params["model"] = model
        params["threshold"] = threshold
        params["pair_limit"] = limit

        similarity = "1 - (va.vector <=> vb.vector)"
        joins = f"""
            from entries a
            join entry_vectors va on va.entry_id = a.id
                                 and va.model = %(model)s
            join entries b on b.id > a.id
            join entry_vectors vb on vb.entry_id = b.id
                                 and vb.model = %(model)s
            where {" and ".join(a_where + b_where)}
              and {similarity} >= %(threshold)s
        """
        with self._cur() as cur:
            cur.execute(f"select count(*) as n {joins}", params)
            total = int(cur.fetchone()["n"])
            if total == 0:
                return [], 0
            cur.execute(
                f"""
                select {_aliased_entry_columns("a", "a_")},
                       {_aliased_entry_columns("b", "b_")},
                       {similarity} as similarity
                {joins}
                order by similarity desc, a.id, b.id
                limit %(pair_limit)s
                """,
                params,
            )
            rows = cur.fetchall()
        return [
            NearPair(
                a=_row_to_entry_prefixed(r, "a_"),
                b=_row_to_entry_prefixed(r, "b_"),
                similarity=float(r["similarity"]),
            )
            for r in rows
        ], total

    def vector_coverage(
        self, query: Query, owner_id: UUID, model: str
    ) -> tuple[int, int]:
        """How many of the population carry a vector for this model.

        A separate query rather than a count inside the pair join: an entry
        with no vector is not in that join at all, so the join can never
        report the entries it is missing.
        """
        where, params = _entry_filters(query, owner_id)
        with self._cur() as cur:
            cur.execute(
                f"""
                select count(v.entry_id) as embedded, count(*) as total
                from entries e
                left join entry_vectors v
                       on v.entry_id = e.id and v.model = %(model)s
                where {" and ".join(where)}
                """,
                {**params, "model": model},
            )
            row = cur.fetchone()
        return int(row["embedded"]), int(row["total"])

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

    def set_memory_collection(
        self, owner_id: UUID, project: str, slug: str | None,
        working_dir: str | None = None,
    ) -> None:
        with self._cur() as cur:
            if slug is None:
                cur.execute(
                    "delete from memory_settings "
                    "where owner_id = %s and project = %s",
                    (owner_id, project),
                )
                return
            cur.execute(
                """
                insert into memory_settings
                    (owner_id, project, collection_slug, working_dir)
                values (%s, %s, %s, %s)
                on conflict (owner_id, project)
                  do update set collection_slug = excluded.collection_slug,
                                working_dir = excluded.working_dir,
                                updated_at = clock_timestamp()
                """,
                (owner_id, project, slug, working_dir),
            )

    def memory_designations(self, owner_id: UUID) -> list[MemoryDesignation]:
        with self._cur() as cur:
            cur.execute(
                "select project, collection_slug, working_dir "
                "from memory_settings where owner_id = %s order by project",
                (owner_id,),
            )
            rows = cur.fetchall()
        return [
            MemoryDesignation(
                project=r["project"],
                collection=r["collection_slug"],
                working_dir=r["working_dir"],
            )
            for r in rows
        ]

    def memory_collection(self, owner_id: UUID, project: str) -> str | None:
        with self._cur() as cur:
            cur.execute(
                "select collection_slug from memory_settings "
                "where owner_id = %s and project = %s",
                (owner_id, project),
            )
            row = cur.fetchone()
        return row["collection_slug"] if row else None

    def set_ingest_paths(
        self, owner_id: UUID, project: str, paths: list[str] | None,
        archive: bool = False,
    ) -> None:
        with self._cur() as cur:
            if paths is None:
                cur.execute(
                    "delete from ingest_settings "
                    "where owner_id = %s and project = %s and archive = %s",
                    (owner_id, project, archive),
                )
                return
            cur.execute(
                """
                insert into ingest_settings
                    (owner_id, project, archive, paths)
                values (%s, %s, %s, %s)
                on conflict (owner_id, project, archive)
                  do update set paths = excluded.paths,
                                updated_at = clock_timestamp()
                """,
                (owner_id, project, archive, list(paths)),
            )

    def ingest_designations(
        self, owner_id: UUID, project: str | None = None
    ) -> list[IngestDesignation]:
        with self._cur() as cur:
            cur.execute(
                "select project, archive, paths from ingest_settings "
                "where owner_id = %s and (%s::text is null or project = %s) "
                "order by project, archive",
                (owner_id, project, project),
            )
            rows = cur.fetchall()
        return [
            IngestDesignation(
                project=r["project"],
                paths=tuple(r["paths"]),
                archive=r["archive"],
            )
            for r in rows
        ]

    # ---------------- ingest runs ----------------

    def start_ingest_run(
        self, owner_id: UUID, project: str, trigger: IngestTrigger,
        archive: bool = False,
    ) -> IngestRun:
        run_id = new_id()
        with self._cur() as cur:
            cur.execute(
                f"""
                insert into ingest_runs (id, owner_id, project, trigger, archive)
                values (%s, %s, %s, %s, %s)
                returning {ingest_run_columns()}
                """,
                (run_id, owner_id, project, str(trigger), archive),
            )
            return _row_to_ingest_run(cur.fetchone())

    def finish_ingest_run(
        self, run_id: UUID, owner_id: UUID, *,
        created: int, changed: int, unchanged: int, swept: int, embedded: int,
        failures: list[dict], twins: list[dict], embed_error: str | None,
    ) -> None:
        with self._cur() as cur:
            cur.execute(
                """
                update ingest_runs
                   set finished_at = clock_timestamp(),
                       created = %s, changed = %s, unchanged = %s,
                       swept = %s, embedded = %s,
                       failures = %s::jsonb, twins = %s::jsonb,
                       embed_error = %s
                 where id = %s and owner_id = %s
                """,
                (created, changed, unchanged, swept, embedded,
                 json.dumps(failures), json.dumps(twins), embed_error,
                 run_id, owner_id),
            )
            if cur.rowcount == 0:
                raise NotOwner(f"ingest run {run_id} is not owned by {owner_id}")

    def latest_ingest_run(
        self, owner_id: UUID, project: str
    ) -> IngestRun | None:
        with self._cur() as cur:
            cur.execute(
                f"""
                select {ingest_run_columns()} from ingest_runs
                 where owner_id = %s and project = %s
                 order by started_at desc
                 limit 1
                """,
                (owner_id, project),
            )
            row = cur.fetchone()
        return _row_to_ingest_run(row) if row else None

    def start_memory_run(
        self, owner_id: UUID, project: str, trigger: MemoryTrigger
    ) -> MemoryRun:
        """The row that exists before any file is read.

        Committed by the caller's autocommit session, which is what makes a
        `finished_at` of null mean "the process died" rather than "the
        transaction rolled back".
        """
        run_id = new_id()
        with self._cur() as cur:
            cur.execute(
                f"""
                insert into memory_runs (id, owner_id, project, trigger)
                values (%s, %s, %s, %s)
                returning {memory_run_columns()}
                """,
                (run_id, owner_id, project, str(trigger)),
            )
            return _row_to_memory_run(cur.fetchone())

    def finish_memory_run(
        self, run_id: UUID, owner_id: UUID, *,
        adopted: int, healed: int, edited: int, regenerated: int,
        deleted: int, unchanged: int, renamed: list[list[str]],
        conflicts: list[str], sidecars: list[str], failures: list[dict],
    ) -> None:
        with self._cur() as cur:
            cur.execute(
                """
                update memory_runs
                   set finished_at = clock_timestamp(),
                       adopted = %s, healed = %s, edited = %s,
                       regenerated = %s, deleted = %s, unchanged = %s,
                       renamed = %s::jsonb, conflicts = %s::jsonb,
                       sidecars = %s::jsonb, failures = %s::jsonb
                 where id = %s and owner_id = %s
                """,
                (adopted, healed, edited, regenerated, deleted, unchanged,
                 json.dumps(renamed), json.dumps(conflicts),
                 json.dumps(sidecars), json.dumps(failures),
                 run_id, owner_id),
            )
            if cur.rowcount == 0:
                raise NotOwner(
                    f"memory run {run_id} is not owned by {owner_id}"
                )

    def latest_memory_run(
        self, owner_id: UUID, project: str
    ) -> MemoryRun | None:
        with self._cur() as cur:
            cur.execute(
                f"""
                select {memory_run_columns()} from memory_runs
                 where owner_id = %s and project = %s
                 order by started_at desc
                 limit 1
                """,
                (owner_id, project),
            )
            row = cur.fetchone()
        return _row_to_memory_run(row) if row else None

    def anchors(self, owner_id: UUID, project: str) -> list[Entry]:
        # `%%` because this statement takes positional parameters, so a
        # literal percent has to be doubled for psycopg's formatter.
        with self._cur() as cur:
            cur.execute(
                f"""
                select {entry_columns("e")} from entries e
                 where e.owner_id = %s
                   and e.project = %s
                   and e.superseded_by is null
                   and e.origin in ('ingested', 'archived')
                   and exists (select 1 from unnest(e.tags) t where t like 'src:%%')
                   and not exists (select 1 from unnest(e.tags) t where t like 'sec:%%')
                 order by e.created_at desc
                """,
                (owner_id, project),
            )
            return [_row_to_entry(r) for r in cur.fetchall()]

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

    def duplicate_unkeyed_events(
        self, owner_id: UUID, limit: int = 20
    ) -> list[DuplicateGroup]:
        """Repeated events that 011's unique index cannot see.

        Scoped to `event_key is null` on purpose. For a keyed event a
        duplicate is already impossible, and asking the same question of
        those rows could only produce false alarms: two tool calls with the
        same payload in one session is ordinary, and it is only the
        harness's own id that says otherwise.

        Payload equality is the signal here, which would be the wrong rule
        for a constraint and is the right one for a report: the worst a
        false positive can do is print a line.

        Tool calls are excluded even when unkeyed. Both harnesses that
        record them stamp a tool_use_id, so an unkeyed one is already
        unusual - and running the same command twice in a session is
        completely ordinary, which would make this fire on healthy data.
        A report nobody can trust is one nobody reads. What is left is
        exactly the two shapes with no id to key on and no reason to
        repeat: claude-code's SessionEnd and opencode's message.
        """
        with self._cur() as cur:
            cur.execute(
                """
                select project, harness, session_id, count(*) as n
                  from events
                 where owner_id = %(owner_id)s
                   and event_key is null
                   and kind <> 'tool_call'
                 group by project, harness, session_id, kind, payload
                having count(*) > 1
                 order by count(*) desc, harness, session_id
                 limit %(limit)s
                """,
                {"owner_id": owner_id, "limit": limit},
            )
            return [
                DuplicateGroup(
                    project=r["project"],
                    harness=r["harness"],
                    session_id=r["session_id"],
                    count=r["n"],
                )
                for r in cur.fetchall()
            ]

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
                -- A duplicate is dropped, not raised. The caller is a
                -- fail-soft hook doing one INSERT; an exception here is how
                -- a twice-registered hook turns into a broken session.
                --
                -- `do update` setting payload to what it already is, rather
                -- than `do nothing`: a no-op write that still RETURNS the
                -- surviving row. `do nothing` returns nothing, and finding
                -- that row afterwards would mean a second copy of 011's
                -- event_key expression here, free to drift from it.
                -- First write wins; nothing about the stored row changes.
                on conflict (owner_id, project, harness, session_id, event_key)
                  where event_key is not null
                  do update set payload = events.payload
                returning id, recorded_at
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
            # The row actually stored, which on a duplicate is the earlier
            # one - so every caller gets a usable Event either way, and the
            # id it carries is the id that is really in the table.
            row = cur.fetchone()
            event.id = row["id"]
            event.recorded_at = row["recorded_at"]
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
        self,
        owner_id: UUID,
        before: datetime,
        force: bool,
        project: str | None = None,
    ) -> tuple[int, int, int]:
        """Delete raw events older than `before`, and say what that cost.

        "Extracted" uses the same watermark rule as
        `sessions_awaiting_extraction` - the newest `covers_through` recorded
        for a session, whatever its job's current status - so prune and
        process can never disagree about what has already been extracted. `--force` (the `force` argument)
        drops that condition entirely rather than widening it: an unextracted
        event is raw that produced nothing, and losing it is the outcome the
        whole pipeline exists to prevent, so overriding that is a deliberate
        act, not a wider window.

        `project` narrows every part of this - the delete AND the
        `kept_unextracted` count - to one project. It has to narrow both:
        counting the whole window while deleting one project's slice would
        refuse a prune because of raw belonging to a project the user never
        named, with nothing in the message to say so. None means every
        project, which is the behaviour that shipped first and stays the
        default; the flag only ever narrows.

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
                  -- Keyed on covers_through, never on status: extract_jobs
                  -- holds one row per session, so a job that succeeded and
                  -- later failed leaves the row FAILED even though its mark
                  -- still stands. covers_through is set only by a successful
                  -- finish and preserved on every failure path, so it is a
                  -- precise record of "extracted through here"; status only
                  -- records how the last run ended. Filtering on status would
                  -- make already-extracted events read as unextracted and
                  -- permanently overcount kept_unextracted.
                  select project, harness, session_id, max(covers_through) as mark
                    from extract_jobs
                   where owner_id = %(owner_id)s and covers_through is not null
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
                     -- One `scoped` CTE feeds both the delete and the
                     -- kept_unextracted count, so filtering here is what
                     -- keeps the two from disagreeing about the window.
                     and (%(project)s::text is null or e.project = %(project)s)
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
                {
                    "owner_id": owner_id,
                    "before": before,
                    "force": force,
                    "project": project,
                },
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
        """Record a job's outcome, and on success clear its retry budget.

        `attempts` counts *consecutive* failures, which is what MAX_ATTEMPTS
        and every docstring around it already claim it means - so a run that
        succeeds resets it to zero. The old capture spool needed no such
        reset: a job was keyed on a transcript path and claimed exactly once,
        so every increment really was a failed try. Discovery-based claiming
        changed that. `claim_extract_job` upserts on the session key, and the
        SAME row is legitimately re-claimed every time the session produces
        new outstanding events - a resumed session (Claude Code keeps its
        session_id across --continue/--resume), or a long one worked over
        successive runs by MAX_EVENTS_PER_JOB. Without the reset, a session
        extracted cleanly more than MAX_ATTEMPTS times dies permanently with
        "gave up after N attempts", a failure that never happened.

        The reset belongs here and not in `claim_extract_job`: claiming stays
        a pure claim, and "a successful run clears the retry budget" sits with
        the rest of the outcome recording, where the next reader will find it.
        """
        with self._cur() as cur:
            cur.execute(
                """
                update extract_jobs
                   set status = %(status)s::job_status, error = %(error)s,
                       entries_written = %(written)s,
                       covers_through = %(covers_through)s,
                       -- Explicitly cast on both uses: the same parameter is
                       -- assigned to a job_status column and compared to a
                       -- text literal, and Postgres refuses to deduce one
                       -- type for both.
                       attempts = case when %(status)s::text = 'done' then 0
                                       else attempts end,
                       updated_at = clock_timestamp()
                 where id = %(id)s and owner_id = %(owner_id)s
                """,
                {
                    "status": str(status),
                    "error": error,
                    "written": entries_written,
                    "covers_through": covers_through,
                    "id": job_id,
                    "owner_id": owner_id,
                },
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

    def transaction(self) -> AbstractContextManager[None]:
        # psycopg's own transaction() already does exactly what the
        # Protocol promises: a real transaction under autocommit, a
        # savepoint inside one already open. No wrapping needed.
        return self._conn.transaction()

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

        The `watermarks` CTE gives the newest covers_through per session -
        the newest, not any, because a session can be extracted more than
        once across its life and only the latest watermark matters. It keys
        on covers_through rather than on job status; see the CTE's comment. Events at or before that mark already produced
        whatever they were going to produce; event_count and the idle check
        both look only at what is left after it, which is what makes
        event_count mean "work outstanding" rather than "events that exist".
        """
        with self._cur() as cur:
            cur.execute(
                """
                with watermarks as (
                  -- Keyed on covers_through, never on status: one row per
                  -- session means a job that succeeded and later failed
                  -- leaves the row FAILED with its mark intact. covers_through
                  -- is written only by a successful finish and preserved on
                  -- every failure path, so it says "extracted through here"
                  -- where status only says how the last run ended. On status
                  -- this session would report every event, extracted ones
                  -- included, as outstanding forever.
                  select project, harness, session_id, max(covers_through) as mark
                    from extract_jobs
                   where owner_id = %(owner_id)s and covers_through is not null
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
