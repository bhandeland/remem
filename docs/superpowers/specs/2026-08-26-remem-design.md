# remem - design

Date: 2026-08-26
Status: approved, ready for implementation planning

## Purpose

A knowledge and memory store for AI coding agents. Agents and humans deliberately
record what they learn about a project - facts, reference docs, and prescriptive
rules - and retrieve it later through ranked search or as a pre-rendered context
block injected at session start.

The v1 goal is a correct, boring substrate: one storage backend, deliberate
writes, keyword search, one agent adapter. Automatic session capture, semantic
search, and team sharing are explicitly deferred, but the schema and interfaces
are shaped so each lands as an addition rather than a rewrite.

## Requirements

Derived from the originating request:

1. A backend storing memories and documentation related to a project. Postgres
   for v1; a file backend may follow behind the same `Store` protocol.
2. Knowledge bases built from memories, to help optimise future agent work.
3. Personal and team knowledge. v1 is personal only; team is modelled, not built.
4. Pluggable agents, starting with Claude Code.
5. Ideas adapted from claude-mem's `cowork` plugin: hook-driven context
   injection, a bundled search skill, fail-soft hooks, a character budget.
6. A command line API for searches and scripting, alongside the MCP server.

## Non-goals for v1

- Automatic capture of session tool-use. The write path and provenance fields
  are designed to accept a capture producer later.
- Semantic / vector search. An `Embedder` seam exists with a null default; the
  Postgres image ships pgvector so the extension is available when wanted.
- Team sharing, grants, roles, permission checks. Ownership is recorded and
  filtered; authorisation is not implemented.
- An HTTP service. The CLI is the scriptable API. A FastAPI layer over
  `services/` remains straightforward later.
- A file/markdown backend.

## Architecture

Layered core with thin frontends. One package, no process boundaries inside
the product.

```
frontends:   cli.py (Typer)      mcp_server.py (FastMCP)     agents/*/hook.py
                 \                     |                          /
services:         write.py  ...  search.py  ...  kb.py
                                   |
store:                    Store protocol (store.py)
                                   |
backend:                  backends/postgres/store.py
```

Frontends parse input and format output. Every decision about knowledge lives
in `services/`. A conditional about knowledge appearing in `cli.py` or
`mcp_server.py` is a defect.

Rejected alternatives:

- *MCP-first, CLI as an MCP client.* Guarantees identical behaviour but adds a
  subprocess and protocol hop to every CLI invocation, and lets the MCP tool
  schema dictate CLI ergonomics.
- *Local daemon, everything a client.* Solves concurrency that Postgres already
  solves. Remains reachable later by lifting `services/` into a server, which is
  a repackaging rather than a redesign.

## Domain model

### Entry

One unit of knowledge, differentiated by `kind`.

| field | type | notes |
|---|---|---|
| `id` | uuid7 | time-sortable, `uuid.uuid7()` is stdlib on 3.14 |
| `kind` | enum | `memory` (something learned) / `doc` (reference) / `rule` (prescriptive convention) |
| `title` | text | |
| `body` | text | markdown |
| `project` | text, nullable | null means global |
| `scope` | enum | `personal` / `team`; always `personal` in v1 |
| `owner_id` | uuid | FK to `principals` |
| `tags` | text[] | |
| `links` | uuid[] | other entry ids |
| `agent` | text, nullable | e.g. `claude-code` |
| `session_id` | text, nullable | null for MCP-written entries; see Provenance |
| `origin` | enum | `human` / `agent` / `capture` |
| `superseded_by` | uuid, nullable | FK to `entries` |
| `created_at`, `updated_at` | timestamptz | |

`rule` is a distinct kind rather than a tagged memory because it renders
differently: rules are always injected and never truncated. That behavioural
difference is what makes a knowledge base prescriptive rather than merely
recalled.

`superseded_by` exists in v1 because the dominant failure mode of agent memory
systems is confidently recalling something that stopped being true. The
alternative - deleting - loses history. Search excludes superseded entries by
default.

### Collection

A knowledge base.

| field | type | notes |
|---|---|---|
| `id` | uuid7 | |
| `slug` | text unique | |
| `title`, `description` | text | |
| `project` | text, nullable | |
| `scope` | enum | as above |
| `owner_id` | uuid | FK to `principals` |
| `query` | jsonb | `{tags: [], kinds: [], project: ...}` |
| `created_at`, `updated_at` | timestamptz | |

Membership is defined two ways simultaneously: explicitly pinned members
(`collection_members`, ordered by `position`) and the stored `query`. Resolution
unions and dedupes both, with pinned entries winning on collision. Pinning is
curation; the query keeps the collection current as entries arrive, and gives a
future "build a knowledge base on topic X" process something to write.

### Principal

| field | type | notes |
|---|---|---|
| `id` | uuid7 | |
| `handle` | text unique | defaults to `brandon` |
| `display_name` | text, nullable | |
| `kind` | enum | `user` / `team` |
| `created_at` | timestamptz | |

Ownership is recorded as a real foreign key from day one so that adding RBAC
later means adding a `grants` table, not backfilling `entries`. `kind = team`
gives the existing `scope = 'team'` value something to point at.

`db up` seeds one principal, resolved from `REMEM_USER_ID`, else config, else
the OS username, defaulting to `brandon`.

## Storage schema

```sql
create type entry_kind   as enum ('memory','doc','rule');
create type entry_scope  as enum ('personal','team');
create type entry_origin as enum ('human','agent','capture');
create type principal_kind as enum ('user','team');

create table principals (
  id uuid primary key,
  handle text not null unique,
  display_name text,
  kind principal_kind not null default 'user',
  created_at timestamptz not null default now()
);

create table entries (
  id uuid primary key,
  kind entry_kind not null,
  title text not null,
  body  text not null,
  project text,
  scope entry_scope not null default 'personal',
  owner_id uuid not null references principals(id),
  tags  text[] not null default '{}',
  links uuid[] not null default '{}',
  agent text,
  session_id text,
  origin entry_origin not null default 'agent',
  superseded_by uuid references entries(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  search tsvector generated always as (
    setweight(to_tsvector('english', title), 'A') ||
    setweight(to_tsvector('english', body),  'B') ||
    setweight(to_tsvector('english', array_to_string(tags,' ')), 'C')
  ) stored
);
create index entries_search_idx  on entries using gin(search);
create index entries_tags_idx    on entries using gin(tags);
create index entries_owner_idx   on entries (owner_id, project);

create table collections (
  id uuid primary key,
  slug text not null unique,
  title text not null,
  description text,
  project text,
  scope entry_scope not null default 'personal',
  owner_id uuid not null references principals(id),
  query jsonb not null default '{}',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table collection_members (
  collection_id uuid not null references collections(id) on delete cascade,
  entry_id      uuid not null references entries(id)     on delete cascade,
  position int not null default 0,
  primary key (collection_id, entry_id)
);
```

The `search` column is generated, so Postgres maintains it on every write.
There is no secondary index to keep in sync and no reindex command.

`tags` and `links` are native arrays rather than join tables: they are small,
always fetched with the entry, and GIN-indexable.

### Access

psycopg 3 with hand-written SQL. No ORM. With a single backend, SQLAlchemy adds
abstraction that is not needed and obstructs `tsvector`, array columns, and a
later pgvector column. The `Store` protocol is already the portability seam.

### Migrations

Numbered `.sql` files under `backends/postgres/migrations/`, applied in order by
a small migrator that records applied versions in a `schema_migrations` table.
Alembic's principal value is autogenerating diffs from ORM models that will not
exist here.

## Services

### `services/write.py`

`remember()`, `update()`, `supersede(old_id, new_entry)`, `link(a, b)`.

`supersede` runs in one transaction: insert the new entry, then set
`superseded_by` on the old one. Nothing is hard-deleted by default.

### `services/search.py`

Accepts a `Query` (`text`, `kinds`, `project`, `tags`, `since`,
`include_superseded`, `limit`) and returns ranked hits.

- Text is compiled with `websearch_to_tsquery`, not `to_tsquery`. It accepts what
  a person or agent actually types (quoted phrases, `or`, `-excluded`) and never
  raises a syntax error on unparseable input.
- Ranking is `ts_rank_cd`, tie-broken by `created_at desc`.
- With no text, it degrades to a filtered list ordered by `created_at desc`, so
  search and list share one code path.
- `owner_id = current_principal` and `superseded_by is null` are applied always,
  not opt-in. Getting the ownership filter right from the first query avoids
  auditing every query later.
- No recency decay in v1. It is a tuning knob with no data behind it, and
  `since` covers the real need.

### `services/kb.py`

*Resolve:* pinned members in `position` order, unioned with the stored query's
matches, deduped, pinned winning on collision.

*Render* produces the context block:

1. Rules first, never truncated.
2. Then docs and memories, whole entries only, filling the remaining character
   budget.
3. A trailing `- N more entries not shown (remem kb show <slug> --full)` line
   whenever anything was dropped.

Each rendered entry carries its id and tags on a metadata line so the agent can
cite or fetch it.

If rules alone exceed the budget, `render` raises rather than dropping them.
A truncated rule is worse than a missing one: the agent proceeds believing it
has the conventions. Silent truncation reads to an agent as a complete picture,
which is why the omitted-count line is mandatory rather than cosmetic.

## Interfaces

### MCP server

FastMCP from `mcp` 2.1.1, stdio transport. Seven tools; every additional tool is
context the agent pays for on every turn.

| tool | purpose |
|---|---|
| `remember(title, body, kind, project, tags, links)` | store a memory, doc, or rule |
| `recall(query, kind, project, tags, limit)` | ranked search returning snippets and ids |
| `get_entry(id)` | full body for a hit worth reading |
| `supersede(id, title, body)` | replace knowledge that stopped being true |
| `kb_context(slug, max_chars)` | rendered context block for a knowledge base |
| `kb_list()` | enumerate knowledge bases |
| `kb_pin(slug, entry_id)` | curate an entry into a knowledge base |

There is deliberately no `update` tool. Typo-fixing does not justify agent
context, and correction is what `supersede` is for; `update` remains in the CLI.

Tool descriptions must state *when* to reach for each tool, not merely what it
does. That text is the only thing steering agent behaviour.

MCP resources are not exposed in v1.

### CLI

Typer.

```
remem db up | migrate | status
    # `up` = create the database if absent, apply migrations, seed the principal.
    # `migrate` = apply pending migrations only. `status` = connectivity plus
    # applied/pending migration versions.
remem remember "title" [--body - | TEXT] [--kind] [--project] [--tag]
remem search QUERY [--kind --project --tag --limit --json]
remem get ID [--json]
remem update ID
remem supersede ID
remem kb list | new | show SLUG [--max-chars | --full] | pin SLUG ID | query SLUG --tag X
remem install claude-code [--scope user|project]
remem hook session-start
remem serve
remem whoami
```

`--body -` reads stdin, so `git log | remem remember "release notes" --body -`
works. `--json` on every read command is the scriptable API from requirement 6:
greppable, jq-able, no service to run.

## Agent adapters

Discovery is via `importlib.metadata` entry points in the `remem.agents` group,
with built-ins registered the same way, so a third party can ship an adapter as
a separate package and have `remem install <name>` find it.

```python
class AgentAdapter(Protocol):
    name: str
    def install(self, scope: Scope) -> InstallReport: ...
    def identity(self, env: Mapping[str, str]) -> Identity: ...
```

### Claude Code adapter

1. **MCP registration.** Prefers `claude mcp add remem -- remem serve` when the
   `claude` CLI is present; otherwise edits `.mcp.json` (project scope) or
   `~/.claude.json` (user scope) directly. Always backs up the file and prints a
   diff before writing.
2. **SessionStart hook.** `remem hook session-start` reads the hook payload on
   stdin, resolves cwd to a project and its knowledge base, and prints the
   rendered context block.
3. **Skill.** A `remem` SKILL.md installed to `~/.claude/skills/`, teaching
   progressive search (broad pass, narrow pass, answer) and, more importantly,
   when writing a memory is warranted. That discipline is what keeps the store
   from filling with noise.
4. **Identity.** `agent = "claude-code"`, with `session_id` and `project` taken
   from the hook payload.

**Fail-soft is a hard requirement on the hook**, adapted from claude-mem: bounded
timeout, exit 0 unconditionally, no output on error. A knowledge tool must never
be the reason a session fails to start.

### Provenance limitation

MCP tool-call payloads carry no session id, so entries written through
`remember` have `session_id = null` unless the environment supplies one.
Hook-written entries carry full provenance; tool-written entries carry partial.
This is recorded accurately rather than fabricated.

## Environment and dependencies

Python 3.14 (3.14.7 verified locally). Verified against PyPI on 2026-08-26:

| package | version | notes |
|---|---|---|
| `mcp` | 2.1.1 | declares 3.14; includes FastMCP |
| `pydantic` | 2.13.4 | declares 3.14 |
| `typer` | 0.27.1 | declares 3.14 |
| `psycopg[binary]` | 3.3.4 | declares 3.14 |
| `platformdirs` | 4.11.4 | declares 3.14 |

Ids use `uuid.uuid7()`, which is in the standard library on 3.14 (verified
locally), so no ULID dependency is needed. All full text search is Postgres
native; there is no local index and therefore no reindex command.

`pgserver` was evaluated and rejected: it publishes no `cp314` wheel (wheels stop
at `cp312`), so it cannot install on Python 3.14. Verified by resolution failure.

Postgres runs via Docker Compose using the `pgvector/pgvector:pg18` image, which
pins both the server version and the vector extension for later use. Docker must
be running; the compose file is the only setup step. Connection settings come
from `REMEM_DSN` or config.

## Configuration

Resolution order for every setting is: environment variable, then config file,
then default.

| setting | env var | default |
|---|---|---|
| Postgres DSN | `REMEM_DSN` | `postgresql://remem:remem@localhost:5433/remem` |
| principal handle | `REMEM_USER_ID` | OS username, else `brandon` |
| config file path | `REMEM_CONFIG` | `platformdirs` user config dir, `remem/config.toml` |
| default context budget | `REMEM_MAX_CHARS` | 6000 |

The compose service binds port 5433 rather than 5432 so remem's database does
not collide with any other Postgres already on the machine.

## Project layout

```
remem/
  pyproject.toml
  compose.yaml
  docs/superpowers/specs/
  src/remem/
    config.py  domain.py  store.py  embedding.py
    backends/postgres/{store.py, migrate.py, migrations/*.sql}
    services/{write.py, search.py, kb.py}
    agents/{base.py, registry.py, claude_code/{adapter.py, hook.py, skill/SKILL.md}}
    mcp_server.py
    cli.py
  tests/
```

## Testing

Test-driven throughout. Real Postgres, no mocking of the store: the backend is
where the interesting behaviour lives, and a mocked store would test nothing.

- A session-scoped fixture creates a scratch database on the compose server and
  runs migrations against it.
- Each test runs inside a transaction that rolls back, giving isolation without
  per-test schema setup cost.
- Database tests are marked and skip with an explicit "start docker compose"
  message when the server is unreachable, so a stopped daemon reads as a skip
  rather than a wall of connection errors.

Coverage priorities: search ranking and filter correctness (especially the
always-on owner and superseded filters), knowledge base resolution and the
render budget rules, supersede transactionality, and hook fail-soft behaviour
under a deliberately broken database.

## Deferred, and how each lands

| deferred | how it arrives |
|---|---|
| Automatic session capture | a producer calling `services/write.py` with `origin = 'capture'` |
| Semantic search | `Embedder` implementation plus a pgvector column and hybrid ranking in `search.py` |
| Team sharing | a `grants` table; relax the always-on owner filter to an authorisation check |
| File backend | a second implementation of the `Store` protocol |
| HTTP API | a thin FastAPI layer over `services/` |
| Topic-driven KB building | a process that writes a query-backed `Collection` |
