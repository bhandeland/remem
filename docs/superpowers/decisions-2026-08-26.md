# remem — implementation decision record

Execution ledger from building remem, 2026-08-26. Preserved because it holds the
reasoning behind 15 rulings that git history does not capture: what was decided,
why, and what it costs if the decision was wrong.

Thirteen of the fifteen rulings resolve defects in the project's own spec or plan
rather than implementation errors — the schema, the migration, the compose file,
and the test harness each shipped a flaw that only surfaced under execution.

Plan: docs/superpowers/plans/2026-08-26-remem.md
Spec: docs/superpowers/specs/2026-08-26-remem-design.md (binding authority)
Built on branch worktree-remem-impl from base commit 16d4021; merged to main at 421acaf.
Final state: 12 tasks, 23 commits, 148 tests passing.

## Pre-flight conflict scan

### Shared files (producer vs consumer)

| Tasks | Shared file | Produces / consumes | Finding |
|---|---|---|---|
| 4 → 5 | backends/postgres/store.py | T4 defines `entry_columns()`, `_row_to_entry`, `PostgresStore`; T5 adds `search` using both | clean (was a defect; fixed in plan self-review) |
| 4 → 7 | backends/postgres/store.py | T7 adds collection methods using `entry_columns("e")`, `_row_to_entry`; adds `_row_to_collection` | clean — T4's import block already carries `json`, `Collection`, `CollectionQuery`, `Scope` |
| 4 → 6 | store protocol | T6 calls `set_superseded(old.id, replacement.id, owner_id)` positionally | clean — names aligned in self-review |
| 7 → 8 | services/kb.py | T8 adds `render`, needs `Kind` | clean — T8 explicitly amends T7's import line |
| 9 → 10, 11, 12 | cli.py | T10 adds `serve`, T11 adds `install`, T12 adds `hook` sub-app | clean — all use `Annotated`/`typer`, both imported by T9 |
| 1 → 3, 11 | pyproject.toml | T3 adds force-include table; T11 rewrites it with both entries | clean — T11 shows the complete table |
| 3 → 9, 10, 12 | tests/conftest.py | T3 defines `db_dsn`/`conn` (rollback) and `live_dsn` (commits); T9/T10/T12 consume `live_dsn` | clean — split added in self-review |
| 1 → 11 | pyproject entry point | T1 declares `remem.agents` → module that exists only after T11 | clean — entry points are lazy; `registry.discover()` swallows load errors |
| 11 → 12 | claude_code/adapter.py | T12's hook calls `.identity(env, payload)` | clean |

### Task self-consistency

| Task | Tests vs code it specifies | Finding |
|---|---|---|
| 1 | config tests vs `load()` | clean — `Path()` accepts both str and Path from `REMEM_CONFIG` |
| 2 | domain tests vs dataclasses | clean |
| 3 | migration tests vs migrator | clean |
| 4 | store tests vs SQL | clean |
| 5 | search tests vs SQL | **DEFECT — see Ruling 1** |
| 6 | service tests vs services | clean |
| 7 | resolve tests vs resolve | clean — empty-query test pins the "matches nothing" semantics |
| 8 | render tests vs render (pure, no DB) | clean |
| 9 | CLI tests vs Typer app | clean — Typer handles StrEnum options by value |
| 10 | MCP tests vs FastMCP | one uncertain API (`mcp.list_tools()`); plan already states the fallback |
| 11 | install tests vs adapter | clean |
| 12 | hook tests vs fail-soft hook | clean |

## Rulings

Ruling 1 (pre-flight, Tasks 3 + 5): `created_at`/`updated_at` default changed from
`now()` to `clock_timestamp()` in 001_initial.sql, and `updated_at = clock_timestamp()`
in UPDATE statements.
— Why: `now()` is transaction time, identical for every row written in one
transaction. Tests run inside a single rolled-back transaction, so two entries
inserted by one test get the SAME created_at, making `order by created_at desc`
non-deterministic. `test_no_text_degrades_to_a_listing_newest_first` (Task 5)
asserts an exact order and would pass or fail at random. `clock_timestamp()`
advances within a transaction, so ordering is stable.
— Cost if wrong: `created_at` reflects statement time rather than transaction
time. For a knowledge store with no multi-row atomic writes that is the desired
semantics anyway. Reversible in one migration.

Ruling 2 (Task 1, plan defect): compose.yaml volume mount changed from
`remem-pgdata:/var/lib/postgresql/data` to `remem-pgdata:/var/lib/postgresql`.
— Why: the plan (my own spec) specified the pre-18 mount point. Postgres 18+
Docker images store data in a subdirectory of /var/lib/postgresql and refuse to
start when a volume is mounted at the old .../data path. Reproduced the failure,
then verified the corrected mount starts cleanly and `create extension vector`
yields pgvector 0.8.6. The Task 1 implementer misdiagnosed this as a Docker
Desktop environment fault; it is not — it is a defect in the plan text.
— Cost if wrong: none identified; this is the mount point the image documents.
Any existing remem-pgdata volume from a failed run must be removed (`docker
compose down -v`), which is harmless as no data has been written yet.

Ruling 3 (Task 3, plan defect — ratifying the implementer's fix): the generated
`search` tsvector column cannot call `array_to_string(tags,' ')` directly. Postgres
requires generated-column expressions to be IMMUTABLE, and `array_to_string` is
STABLE. Controller verified independently: `select provolatile from pg_proc where
proname='array_to_string'` returns 's' for both overloads. The plan's SQL — my own —
could never have applied.
— Fix ratified: a schema-local `remem_array_to_string_immutable(text[], text)` SQL
function marked IMMUTABLE STRICT PARALLEL SAFE, used in place of the bare call.
Controller verified in a throwaway schema that a generated column built this way
accepts the insert AND that `search @@ websearch_to_tsquery('english','kubernetes')`
matches a row whose only occurrence of that word is in `tags` — i.e. Task 5's
`test_tags_are_searchable_text` will be satisfiable.
— Why this over the alternatives: dropping tags from the tsvector would violate the
spec's weight-C tags clause and break tag search; a trigger-maintained column would
contradict the spec's stated reason for using a generated column (nothing to keep in
sync, no reindex command). Marking the wrapper IMMUTABLE is sound for `text[]`
specifically — the generic function is STABLE only because non-text element types
have setting-dependent output functions (e.g. DateStyle for timestamps).
— Cost if wrong: the schema carries one extra function that must exist before
`entries`. Contained in the same migration; reversible.

Ruling 4 (Task 4 → Tasks 5, 7): accept the implementer's trim of unused imports from
backends/postgres/store.py, and carry the consequence forward explicitly.
— What happened: Task 4 dropped `json`, `Collection`, `CollectionQuery`, `Hit`, `Query`
from the store's import block, since none of its five methods use them. Defensible and
tests pass. But my pre-flight scan marked rows "4 → 5" and "4 → 7" CLEAN specifically
because those imports were present, and both later tasks append code referencing them
(Task 5 needs `Query`/`Hit`; Task 7 needs `json`/`Collection`/`CollectionQuery`).
That scan row is now void.
— Decision: keep the trim rather than restoring dead imports, and instruct Tasks 5 and 7
in their dispatches to add exactly the imports they need. A NameError at first test run
would have caught this anyway, but only after wasting a fix round.
— Cost if wrong: none material; worst case is one extra fix round in Task 5 or 7.

Ruling 5 (Task 4, plan-mandated finding — FIX, do not park): `set_superseded` writes
`superseded_by = new_entry_id` with no check that the new entry belongs to the same
owner. The old row IS correctly owner-scoped (verified: the reviewer confirmed
`test_set_superseded_refuses_across_owners` genuinely fails if the filter is dropped),
so this is a data-integrity gap, not a data leak — a supersession chain can point at
another principal's entry.
— Decision: fix now rather than park. The spec states ownership must be enforced
consistently from the first query precisely so that team scope later means relaxing one
filter instead of auditing every statement. This is a one-clause SQL change plus one
test, and it is cheap now and expensive once `grants` exists. The service layer (Task 6)
never produces a cross-owner link, so the hole is only reachable by calling the store
directly — which is exactly the kind of latent gap that survives to production.
— Cost if wrong: a superseding entry must now exist and be owned by the caller, so a
caller cannot point at an entry they cannot see. No legitimate use case is lost.

Ruling 6 (Task 4, plan-mandated finding — FIX): `ensure_principal` uses
`ON CONFLICT (handle) DO UPDATE SET handle = excluded.handle`, which rewrites the row on
every call, producing a dead tuple each time even though nothing changes.
— Decision: fix. This is the hottest path in the system: `open_session()` calls
`ensure_principal`, and the MCP server opens a session on EVERY tool call. Every agent
tool call would write a dead tuple to `principals`. Autovacuum would cope, but the write
amplification is pure waste on the one query guaranteed to run constantly.
— Replacement: SELECT first and return on hit; otherwise INSERT ... ON CONFLICT DO
NOTHING, then SELECT again (the second select also handles a concurrent creator).
— Cost if wrong: two round-trips instead of one on first-ever call for a principal; one
round-trip on every call thereafter, down from one write. Strictly better on the hot path.

Ruling 8 (Task 7, CRITICAL plan-mandated defect — FIX, and amend the migration in place):
`collections.slug` was declared globally `unique`, but collections are owner-scoped. With
`put_collection`'s `ON CONFLICT (slug) DO UPDATE`, one principal reusing another's slug
silently overwrites that collection's title, description, project, scope, and query while
leaving `owner_id` untouched — so the victim keeps ownership of corrupted data and the
writer cannot even see the row they think they created. Cross-tenant data corruption,
no error raised. The flaw is in my spec's schema, not the implementer's work; no test
exercised two owners sharing a slug, so it shipped uncaught.
— Fix: `unique (owner_id, slug)` in 001_initial.sql, `ON CONFLICT (owner_id, slug)` in
put_collection, plus a regression test with two principals and one shared slug.
— Amending 001 rather than adding 002: controller verified no persistent database has
ever applied 001 (`\dt` on the dev database returns no tables; the only `remem%` database
is the empty dev one — test databases are scratch and dropped). Shipping a 002 to repair
a constraint that never existed anywhere would be worse than fixing the origin.
— Cost if wrong: any database that HAD applied the old 001 would need recreating. Verified
none exists. If one appears later, `docker compose down -v` is the remedy and no data is
lost, as nothing has been written yet.

Ruling 9 (Task 5 code, found by controller hands-on CLI testing — FIX in the final wave):
search snippets carry `ts_headline`'s default HTML markup. `remem search --json` returns
`"snippet": "Run <b>migrations</b> before restarting workers."`
— Why it matters: the snippet field is consumed by (a) `--json`, the documented scriptable
API, and (b) the MCP `recall` tool, whose output goes straight into an AI agent's context.
HTML bold tags are noise in both — wasted tokens for the agent, markup to strip for any
script. No test caught this because every test asserts substring containment, which `<b>`
tags do not disturb. Only visible by running the CLI and reading the output.
— Fix: pass `StartSel=, StopSel=` in the `ts_headline` options string so the excerpt comes
back as plain text. Batched into the final fix wave rather than a fix round, since it is
cosmetic and a second implementer was mid-flight.
— Cost if wrong: search results lose match highlighting. Nothing consumes the highlight
today, and a terminal reader still gets the relevant excerpt.

Ruling 10 (Task 9, Important plan-mandated — FIX in the final wave): a malformed entry id
produces a raw Python traceback. Controller reproduced: `remem get abc` prints a full Rich
traceback ending in `ValueError: badly formed hexadecimal UUID string`.
— Cause: `get`, `update`, `supersede`, and `kb pin` all call `UUID(entry_id)` directly on
user input with no guard. The brief's code never wrapped it.
— Why fix: this is the primary CLI's response to an ordinary typo. The project's own error
convention elsewhere is a clean stderr message plus a non-zero exit — `get` already does
exactly that for an id that is well-formed but absent. A traceback for a typo is worse
than the behaviour two lines away in the same function.
— Fix: parse the id through a small helper that catches ValueError and exits with
`No entry '<value>'` (or `not a valid id`) on stderr, non-zero. Apply to all four call sites.
— Cost if wrong: none identified; strictly better UX. Batched into the final fix wave.

Task 9: minor (deferred): `ensure_database` interpolates the database name into
  `create database "<name>"` rather than parameterizing (Postgres cannot parameterize
  identifiers). Reviewer judged it practically unexploitable — whoever controls the DSN
  already holds admin rights on that connection. Accepted as-is.
Task 9: complete (commits f5247f6..36c1682, spec ✅, quality approved; 90 passed; controller
  independently ran the CLI end-to-end: db up on a nonexistent DB, remember, search --json,
  kb new, kb show, whoami — all correct)

Ruling 11 (Task 10, plan defect — ratify with one change): the spec and plan both say
"FastMCP from `mcp` 2.1.1". That class does not exist under that name. Controller verified
on the installed package: `import mcp.server.fastmcp` raises ModuleNotFoundError whose own
message states "This is mcp 2.x, where FastMCP was renamed to MCPServer (from
mcp.server.mcpserver import MCPServer)". `list_tools()` is still an async method returning
objects with `.name`, so the brief's registration test stands unmodified.
— Ratified: use `MCPServer`. BUT drop the implementer's `as FastMCP` alias — importing
`MCPServer as FastMCP` makes every call site name a class that does not exist, which is
how the next reader inherits my mistake. Use the real name.
— Cost if wrong: none; this is the documented v2 API. Note for Task 12: the README must
say MCPServer, not FastMCP.

Ruling 12 (Task 10, CROSS-CUTTING test-isolation defect in my plan — FIX): the full suite
went red (97/98). `test_cli.py::test_supersede_replaces_and_hides_the_old_entry` and
`test_mcp_server.py::test_supersede_hides_the_old_entry_from_recall` both commit into the
SAME session-scoped `live_dsn` database and both use the identical strings "Fridays" /
"deploy fridays" / "Tuesdays". test_cli runs first alphabetically and leaves its row
committed, so the MCP test's `recall(query="deploy")` returns two hits and fails
`== ["Tuesdays"]`. Deterministic, not flaky; passes standalone.
— This is my defect twice over: I designed `live_dsn` as a shared, never-reset database,
and I wrote both tests with the same distinctive sample text.
— Decision: fix the CAUSE, not the symptom. Renaming one test's strings would leave the
hazard armed for the next commit-based test file (Task 12's hook tests are next, and they
also commit). Add a function-scoped autouse fixture in conftest.py that truncates the data
tables before any test that uses `live_dsn`, tolerating the not-yet-migrated case.
— Rejected: making `live_dsn` function-scoped (a create+drop database per test is slow and
re-runs migrations every time); editing the brief-mandated test text (hides the defect).
— The implementer was right to escalate rather than edit another task's files unasked.
— Cost if wrong: a truncate per commit-based test — microseconds on tables this size.

Ruling 13 (Task 11, two CRITICAL plan-mandated defects — FIX): the installer's backup
logic does not protect the user's real config files.
(a) `_install_mcp` backs up only when NO `.claude.json.bak*` file already exists. That
condition is permanent once true, so only the FIRST install ever takes a snapshot. A user
who installs, hand-edits `.claude.json`, then re-installs is overwritten with no current
backup — the only backup on disk predates their edits. Reviewer verified empirically.
(b) `_install_hook` never calls `_backup()` at all on the common path. `settings.json`
only gets backed up via `_read_json`'s corrupt-file branch, so a valid settings.json
holding the user's `permissions`, `model`, `statusLine` is overwritten with zero backups.
— Both are my brief's code, not implementer error. In-memory merging means no data is
lost today; the defect is that the user's recovery path does not exist if the merge is
ever wrong. For code that edits a live Claude Code configuration, that is not acceptable.
— Fix: back up immediately before EVERY write to either file, with collision-safe naming
(same-second installs must not overwrite each other's backup). Treat both files identically.
— Cost if wrong: a few extra .bak files in the user's home. Installs are rare, and the
alternative failure mode is silent destruction of their configuration.

Ruling 14 (Task 11, Important — FIX): `registry.discover()` catches bare `Exception` and
`continue`s on every entry-point load failure, with no diagnostics. That correctly stops a
broken THIRD-PARTY adapter from breaking remem, but it applies equally to remem's own
bundled Claude Code adapter — an ImportError there would present as "unknown agent
'claude-code'. Available: none", sending the user to debug the wrong thing entirely.
— Fix: keep swallowing (robustness is the point) but emit a warning naming the adapter and
the underlying error, so the failure is diagnosable instead of invisible.
— Cost if wrong: one stderr line in a situation that is already broken.

## Progress

Task 1: fix round 1/5 (1 addressed, 0 open — compose volume mount for pg18; commits dbe8c07..00a671c)
  Controller verified independently: `docker compose ps` shows healthy, port 5433 mapped.
  Note: implementer's original report misdiagnosed this as a Docker Desktop fault.
Task 1: minor (resolved, not deferred): reviewer flagged `.superpowers/` in .gitignore as
  unrequested scope — it was requested, by the controller's dispatch, to keep SDD scratch
  artifacts out of the repo. Reviewer lacked that context. No action.
Task 1: minor (deferred): implementer's first report contained a self-contradiction — the
  completion checklist claimed the database was healthy while its own Concerns section said
  it failed to start. Process note for the final review, not a code defect.
Task 1: complete (commits 16d4021..00a671c, review clean — spec ✅, quality approved)
Task 2: note (informational): CollectionQuery defines a custom __eq__, which sets
  __hash__ = None — instances are unhashable. Reviewer verified the custom __eq__ DOES
  survive @dataclass(slots=True). Not a defect; matters only if a later task tries to
  put CollectionQuery in a set or dict key. No action.
Task 2: complete (commits 00a671c..f258fb1, review clean — spec ✅, quality approved)
Task 3: minor (deferred): `migrate()` has no docstring note that the CALLER owns
  commit/rollback. Reviewer proved the batch is all-or-nothing only if the caller rolls
  back on exception. Task 9 wires this into session.py — watch for it there.
Task 3: minor (deferred): `schema_migrations.applied_at` still defaults to `now()`, not
  `clock_timestamp()`, inconsistent with Ruling 1. Harmless today (nothing orders by it).
Task 3: complete (commits f258fb1..4d82e57, review clean — spec ✅, quality approved)
Task 5: complete (commits 7a38c03..20ed34b, review clean — spec ✅, quality approved,
  zero findings). Note: implemented before Task 4's fix round closed; reviewed against a
  frozen diff snapshot to avoid the concurrent edit to store.py.
Task 4: fix round 1/5 (2 findings addressed per Rulings 5 and 6; commits 7a38c03..0ac44d9;
  40 passed incl. 1 new test for the cross-owner supersede case)
Task 4: complete (commits 4d82e57..0ac44d9, re-review clean — both findings ADDRESSED,
  no new breakage)
Ruling 7 (Task 6): accept the implementer's addition of a `RuntimeError` guard when
`store.set_superseded()` returns False, despite the branch being unreachable by
construction and therefore untested.
— Why: the brief discarded that boolean. Ignoring it means a failed supersession returns
a replacement entry that was never linked as the successor — a silent failure that looks
identical to success. The project's stated values are explicit about reporting outcomes
faithfully and never truncating silently; this is the same principle one layer down. The
task reviewer independently reached the same conclusion.
— Cost if wrong: two lines of defensive code that never execute. Cheap to delete.

Task 6: minor (deferred): `update()` cannot explicitly CLEAR `project` — `None` means
  "unchanged", so there is no way to un-set it. Plan-mandated. Needs a sentinel default
  or a separate clear operation if that turns out to matter.
Task 6: minor (deferred): `supersede()` makes two store calls, not one explicit
  transaction. In practice they share one psycopg connection with autocommit off, so they
  commit together — BUT only if the caller does not catch the exception and commit anyway.
  Same underlying property as the Task 3 `migrate()` note. VERIFIED SAFE for Task 9's
  `open_session`, which skips its `conn.commit()` on exception and lets the connection
  context manager roll back.
Task 6: minor (deferred): `link()` does not guard `a_id == b_id`; a self-link is recorded
  silently and is untested.
Task 6: minor (deferred, genuine gap not required by the brief): no services-layer test
  for cross-owner rejection. Covered transitively by store-level tests.
Task 6: minor (deferred): `test_find_caps_an_absurd_limit` does not actually prove the
  200-cap fires — only 3 rows exist, so it would pass either way.
Task 6: complete (commits 0ac44d9..36bb873, review clean — spec ✅, quality approved)

Task 7: complete (commits 36bb873..f5247f6, spec ✅; one CRITICAL fixed per Ruling 8,
  pre-fix failure reproduced and evidenced; 79 passed)
Task 7: complete (commits 36bb873..f5247f6, re-review clean — both findings ADDRESSED,
  no new breakage, no stray 002 migration, render() untouched)
Task 8: minor (deferred): `render()` output can exceed `max_chars` by up to the length of
  the omission notice line, which is appended after the budget loop and not counted
  against it. Bounded and small (~60 chars against a 6000 default). Plan-mandated.
Task 8: minor (deferred, FLAG TO FINAL REVIEW — genuine coverage gap on the headline
  behaviour): `test_rules_are_never_truncated_even_over_budget` passes max_chars=100000
  for three small rules, so it cannot fail — nothing is under budget pressure. Combined
  with `test_rules_render_before_other_entries` also running everything under-budget
  (5000), NO test actually proves rules survive when non-rule entries are being dropped.
  That is the single most important behaviour in the task. Needs a test with a small
  budget, several rules, and many large non-rule entries, asserting every rule is present
  and the omission notice fired. Deferred to the final fix wave only because a second
  implementer was already running on another file.
Task 8: complete (commits 231cde4..92d0670, review clean — spec ✅, quality approved)

Task 10: fix round 1/5 (test isolation + FastMCP alias per Rulings 11-12; commits
  c8830ae..5eeccf3; 98 passed, verified independently by controller)
Task 10: fix round 2/5 (2 CRITICALs addressed — kb_pin ForeignKeyViolation, invalid-kind
  ValueError; commit bc94132; 120 passed on two consecutive runs; all 3 new tests evidenced
  failing pre-fix). Re-review: both ADDRESSED, no new breakage; confirmed the pre-check uses
  the OWNER-SCOPED get_entry form, closing the cross-owner pinning gap as a bonus.
Task 10: complete (commits 36c1682..bc94132, re-review clean)

Task 11: fix round 1/5 (2 CRITICALs + 1 Important addressed per Rulings 13-14; commit
  1e3571f). Re-review: all three ADDRESSED, no new breakage; all 11 install() call sites
  verified passing home=tmp_path, so no test can reach the real config.
Task 11: complete (commits 5eeccf3..1e3571f, re-review clean)
Task 12: implemented (commit 32e0fd0). Controller independently verified fail-soft twice:
  DB stopped -> exit 0, 0 bytes output; malformed stdin -> exit 0, 0 bytes output.
  Full suite 124 passed on the controller's own run.
Task 12: minor (deferred, FOR FINAL FIX WAVE): no diagnostic path at all. A hook that runs
  on every session start and is silent by design means a genuinely broken remem is
  indistinguishable from "no knowledge base configured" — forever, with no way to find out.
Task 12: minor (deferred, FOR FINAL FIX WAVE): project resolution uses cwd's BASENAME, so
  two different checkouts named the same share a knowledge base. Undocumented in the README.
Task 12: minor (parked): `except Exception` rather than `BaseException` leaves a theoretical
  SystemExit/KeyboardInterrupt gap in "exit 0 unconditionally". No realistic trigger.
  Ruling: leave it — catching BaseException would swallow Ctrl-C, which is worse.
Task 12: minor (parked): `main()`'s own try/except is not independently exercised; both
  main()-level tests pass even if that guard is deleted, because session_start()'s guard
  already covers them. Ruling: real but low value — it is a redundant second layer, and the
  behaviour it guards IS covered.
Task 12: complete (commits 1e3571f..32e0fd0, review clean — spec ✅, quality approved)

ALL 12 TASKS COMPLETE. 124 tests passing.

FINAL WHOLE-BRANCH REVIEW (opus, 18 commits): no Critical findings. Merge recommended
after one fix wave. Fitness verdict: "a coherent v1 of what the spec describes".

Ruling 15 (final review): adopt all nine of the reviewer's fix-wave items rather than
deferring any. Rationale — three were already slated (rules-under-budget test, ts_headline
markup, malformed-id traceback), and the other six are each one-file or one-function
changes whose absence would be felt in a user's first hour. The three most important were
invisible to every per-task review by construction:
  (a) `put_entry` had no owner guard on its ON CONFLICT path — the LAST unfixed member of
      the family that produced Rulings 5 and 8. Reproduced: a second principal overwrote
      another's entry while ownership stayed unchanged. Three tasks each held one member of
      this family; no single-task reviewer could see the pattern.
  (b) The pin decision was duplicated in cli.py and mcp_server.py and had DIVERGED — Task
      10's fix round hardened the MCP copy, the CLI copy kept the bug, because no task's
      diff contained both files. Fixed as one `kb.pin` service used by both.
  (c) `db migrate` / `db status` crash on an unmigrated database — the exact state they
      exist to repair and report. My plan self-review caught this for `db up` and did not
      carry the fix to its siblings.
— Cost if wrong: the wave touches working code late. Mitigated by one scoped re-review and
a mandatory re-verification of the hook's fail-soft behaviour.

Deferred as follow-ups (reviewer concurred): `remem kb query` (kb new's upsert warns
instead), the Embedder seam, and the Minor interface-drift items.

Fix wave: 5 commits 32e0fd0..421acaf, all nine items applied, 148 passed (was 124).
Controller verified independently, by hand: snippets carry no `<b>` markup; `remem get abc`
prints `'abc' is not a valid entry id`; with Postgres stopped `remem search foo` prints the
docker-compose message and exits 1; and the hook with Postgres stopped still exits 0 with
ZERO bytes of stdout — fail-soft did not regress.

Parked (fix-wave implementer's own notes, none blocking):
- `store.pin` still returns None, so a cross-owner pin writes nothing silently. Unreachable
  today because `kb.pin` pre-checks; would want a bool if a caller ever skips the service.
- `db status` creates `schema_migrations` as a side effect (via `applied_versions`), so
  inspecting an unmigrated database writes to it. Pre-existing, harmless, mildly surprising.
- `services/kb.py` now imports `EntryNotFound` from `services/write.py` — a new
  service-to-service edge, chosen over duplicating the exception, which would force
  frontends to catch two types for one condition. Agreed.
- `remem kb query` remains unimplemented: a knowledge base's query is still write-once, and
  `kb new`'s silent upsert is the only repair path. Item 9(c) warns at creation time.
  Accepted as a follow-up by the final review; flagged here so it is not lost.

Controller error (no ruling needed): dispatched Task 6 before generating its brief. The
  implementer correctly refused to improvise requirements and asked. Brief generated,
  re-dispatched. Cost: one wasted dispatch.


---

## Parked items — closed 2026-08-26 (post-merge)

Every item parked above was revisited. Outcome:

FIXED:
- `update()` could not clear `project` — added a `CLEAR` sentinel (`None` still
  means "unchanged"), plus `--clear-project` / `--clear-tags` on the CLI.
- `link()` silently recorded a self-link — now raises `CannotLinkToSelf`.
- `store.pin` returned `None`, so a failed ownership guard wrote nothing
  silently — now returns bool, matching `set_superseded`.
- `db status` created `schema_migrations` as a side effect of reading it —
  `applied_versions()` is now read-only via a `to_regclass` guard.
- `schema_migrations.applied_at` used `now()` — now `clock_timestamp()`, matching
  Ruling 1. Existing databases keep the old default; nothing orders by it.
- `migrate()` had no docstring saying the CALLER owns commit/rollback — the
  property that makes the batch all-or-nothing is now written down.
- `render()` could exceed `max_chars` by the length of the omitted-count notice —
  the notice is now budgeted, dropping a further entry if needed.
- `ensure_database` hand-quoted the database name — now `psycopg.sql.Identifier`.
- No services-layer cross-owner coverage — added for update/supersede/link. The
  behaviour was already correct; only the proof was missing.
- `test_find_caps_an_absurd_limit` could not fail (3 rows, cap 200) — rewritten
  with MAX_LIMIT+25 rows so the cap is observable.
- `main()`'s outer try/except was never exercised — two tests now make
  `session_start` raise and make `sys.stdin.read()` raise.
- `remem kb query` did not exist, so a knowledge base's query was write-once and
  `kb new`'s silent upsert was the only repair. Added `kb.set_query` plus
  `remem kb query <slug> [--tag/--project/--kind] [--clear]`. Title, description
  and pins are preserved.

WON'T FIX (ruling stands):
- Hook catches `Exception` rather than `BaseException`. Catching BaseException
  would swallow Ctrl-C, which is worse than the theoretical gap it closes.
- `services/kb.py` importing `EntryNotFound` from `services/write.py`. The
  alternative — a second identically-named exception — would force every
  frontend to catch two types for one condition.
- Task 1's self-contradicting implementer report is a historical process note,
  not a code defect.
