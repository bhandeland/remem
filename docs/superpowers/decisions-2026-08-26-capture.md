# SDD ledger — plan: docs/superpowers/plans/2026-08-26-capture.md

Spec: docs/superpowers/specs/2026-08-26-capture-design.md (read; binding authority)
Worktree: .claude/worktrees/capture-impl on branch worktree-capture-impl
Base commit: bed4280

## Pre-flight conflict scan

### Shared files (producer vs consumer)

| Tasks | Shared file | Produces / consumes | Finding |
|---|---|---|---|
| 1 → 2 | domain.py | T1 adds `Query.origins`; T2 filters on it | clean |
| 1 → 5 | domain.py | T1 adds `CaptureJob`/`CaptureStatus`; T5 persists them | clean |
| 2 → 5 | backends/postgres/store.py | T2 edits `search`/`fuzzy_search`; T5 appends capture methods | clean — disjoint methods, sequential tasks |
| 2 → 6 | store.search | T6's `_already_captured` passes `origins=[CAPTURE]` with no text | clean — T2 adds the filter above the text branch, so the listing path honours it |
| 3 → 4 | distill/ | T3 defines `CapturedEntry`/`parse_entries`; T4 consumes | **DEFECT — Ruling 2** |
| 3 → 6 | distill/base.py | T6 imports `Distiller`, `DistillationFailed`, `CapturedEntry` | clean |
| 5 → 6 | Store protocol | T6 calls all eight capture methods | clean — signatures match T5's produces block |
| 6 → 7 | services/capture.py | T7 calls `capture.enqueue` | clean — imported inside the function, avoiding a cycle |
| 7 → 9 | hook.py | T9's `spawn_drain` uses `CHILD_ENV_VAR` from T7 | **DEFECT — Ruling 2** |
| 7 → 8 | cli.py / adapter | T8 wires `hook session-end` and registers the SessionEnd hook | clean |
| 8 → existing | adapter.py | `_install_hook` refactor must keep the SessionStart assertions in the earlier plan's tests passing | clean — refactor preserves shape and idempotence |

### Task self-consistency

| Task | Tests vs code it specifies | Finding |
|---|---|---|
| 1 | schema + dataclass tests | clean |
| 2 | origins filter in both paths | clean |
| 3 | `parse_entries` tests vs implementation | **DEFECT — Ruling 3** |
| 4 | subprocess asserted, never executed | minor — `import os` inside the method (Ruling 4) |
| 5 | claim/finish/counts, SKIP LOCKED, stale reclaim | clean — `make_interval(secs => ...)` is valid Postgres |
| 6 | service tests vs service | clean; dedup scans at most 200 captured entries per project (noted below) |
| 7 | hook tests vs hook | clean |
| 8 | CLI + installer tests | clean |
| 9 | spawn_drain tests | clean |

## Rulings

Ruling 1 (pre-flight, environmental — carry into EVERY dispatch): implementers must
NOT run `docker compose up -d` from this worktree. Postgres is already running and
healthy at localhost:5433 under compose project `remem` (verified: container
`remem-db-1`, and a psycopg connect from inside the worktree succeeds). Compose derives
its project name from the directory, so running it here would create a SECOND container
under project `capture-impl` that fails to bind 5433 and leaves a broken container
behind. The controller hit exactly this failure earlier in the session.
— Cost if wrong: none. The database is reachable; there is nothing to start.

Ruling 2 (pre-flight, Tasks 3/4/7/9): `CHILD_ENV_VAR = "REMEM_CAPTURE_CHILD"` is defined
twice in the plan — once in `distill/claude_cli.py` (Task 4) and once in
`agents/claude_code/hook.py` (Task 7). Two definitions of one magic string that MUST
agree, in the guard whose failure mode is unbounded recursion.
— Decision: define it ONCE, in `src/remem/distill/base.py` (Task 3), and import it in
Task 4, Task 7, and Task 9. `distill/base.py` is layer-neutral: the hook importing a
constant from it does not couple the hook to subprocess machinery, and the distiller
importing from `agents/` would invert the layering.
— Cost if wrong: one extra import in two modules. Cheap against the alternative, where a
later edit to one copy silently disarms the recursion guard.

Ruling 3 (pre-flight, Task 3 — plan self-contradiction): the plan's parametrised test
expects `parse_entries("[1,2,3]")` to raise `DistillationFailed`, but the specified
implementation drops each non-object item and returns `[]`. The test as written fails.
— Decision: `parse_entries` raises `DistillationFailed` when the array is NON-EMPTY but
no item validates. An empty array stays a success (spec: "an empty array is a success").
— Why this direction rather than dropping the case from the test: `[]` means the model
deliberately found nothing durable, which is the common correct answer. A non-empty array
where nothing survives validation means the model produced output we could not use — that
is a distillation failure and should be visible in `capture status`, not silently
recorded as a clean session with zero entries. Collapsing the two would make a broken
prompt indistinguishable from a quiet week.
— Cost if wrong: a session whose distillation was entirely malformed shows as `failed`
rather than `done`. That is the more informative of the two.

Ruling 4 (pre-flight, Task 4 — minor): the plan puts `import os` inside
`ClaudeCliDistiller.distill`. Move it to module level.
— Cost if wrong: none; it is a style correction that avoids a wasted review round.

## Known limits accepted at pre-flight

- Dedup (`_already_captured`) scans at most 200 captured entries per project. A project
  with more than that could admit a duplicate title. Accepted for v1: the alternative is
  an indexed existence query, which is a reasonable follow-up but not worth widening
  Task 6's scope now.

Ruling 5 (Task 1 review, Important — FIX): `services/search.py::_clamped()` rebuilds a
`Query` field by field and does NOT copy `origins`. A search with an origin filter AND a
limit above MAX_LIMIT silently loses the filter.
— Not reachable today (`kb.resolve` passes limit=200 == MAX_LIMIT, so `_clamped` returns
unchanged; no current caller passes both), but it is a latent trap in the exact mechanism
that keeps captured entries out of context blocks. The next caller to pass origins through
`find()` with a large limit would get captures back with no error.
— This is the same defect class as several already fixed in this project: a filter that
holds on one path and silently drops on another.
— Cost if wrong: none; copying one more field through a rebuild that already copies six.

Ruling 6 (Task 1 review, Important plan-mandated — FIX): the partial-index test asserts
only `"pending" in indexdef`, which passes trivially because the index is NAMED
`capture_jobs_pending_idx`. The reviewer proved this by building a same-named index with
NO partial predicate and watching the assertion still pass.
— My plan wrote that test. The migration itself is correct (the real index does carry
`WHERE (status = 'pending'::capture_status)`), so there is no functional bug today — but
the regression protection is theatre, and this project has already shipped one test that
could not fail (the rules-under-budget test caught in the last final review).
— Fix: assert on the predicate, not the bare word.
— Cost if wrong: none.

Ruling 7 (Task 3, CRITICAL plan-mandated — FIX): my extraction regex
`re.compile(r"\[.*\]", re.DOTALL)` is greedy, so it always spans from the FIRST `[` in the
output to the LAST `]`, not to the array's own closing bracket. The reviewer reproduced two
ordinary cases that raise `DistillationFailed` on perfectly good output:
  - a model second-guessing itself: `First attempt: [1,2,3] was wrong. Correct: [{...}]`
  - a fenced block followed by a sentence containing a bracket: `...``` \n Note: see items[0]`
The brief's tests never put a second bracket pair outside the target array, so the gap was
invisible to them. The reviewer searched for a case that extracts a PARSEABLE-but-wrong span
and could not find one — the failure mode is "reject good output", not "silently accept the
wrong array". Smaller than corruption, still a real bug.
— Why fix rather than park: this interacts with Ruling 3. That ruling makes
`DistillationFailed` a meaningful signal in `capture status` ("the model produced output we
could not use"). A regex that raises on GOOD output poisons exactly that signal, and would
suppress real entries while looking like a model problem.
— Fix: replace the regex with a scan that collects every span decodable as a JSON list via
`json.JSONDecoder().raw_decode`, then returns the FIRST candidate yielding at least one
valid entry; failing that, returns `[]` if any candidate was an empty list (the deliberate
"nothing durable" answer); failing that, raises. Taking merely the first parseable array
would pick the decoy `[1,2,3]` in the reviewer's own example, so first-valid is the rule,
not first-parseable.
— Cost if wrong: a model emitting two genuinely valid arrays gets the first. No worse than
the current all-or-nothing behaviour, and the prompt asks for exactly one array.

Ruling 8 (Task 6, CRITICAL plan-mandated — FIX): `drain`'s docstring promises "Never
raises: a failing job records its reason", but my plan only wrapped the transcript read and
`distiller.distill(...)`. Unguarded: `_write()` (which calls `remember` -> `put_entry`, a
raw `cur.execute`) and ALL FOUR `finish_capture_job` calls. Any transient database error —
exactly what a background job runner must be most defensive about — escapes `drain`,
aborting the loop so jobs already claimed in the same batch are never processed and never
have their status recorded. Worse, a failure partway through `_write` leaves entry 1
written, the job stuck in `running` with no `error`, and nothing surfaced to
`capture status`; it is only revisited by stale-reclaim, silently incrementing attempts.
— No test exercised it, so nothing caught it before this review.
— Fix: wrap the ENTIRE per-job body, and make the status write itself best-effort so a
failure recording a failure cannot abort the drain either.
— Cost if wrong: a genuinely dead database now yields a drain that reports every job failed
rather than raising. That is the correct behaviour for a background runner and is what the
CLI's own error handling expects.

Ruling 9 (Task 7, Important plan-mandated — FIX): the recursion-guard test asserts that
`capture.enqueue` is never called when `REMEM_CAPTURE_CHILD=1`. But if the guard were
deleted, `session_end` would proceed to `open_session`, and in a DSN-unreachable
environment that raises, gets swallowed by the fail-soft handler, and `enqueue` is STILL
never called — so the test passes with the guard gone. Its validity depends on the ambient
database being live. The reviewer verified this empirically.
— Why fix rather than defer: this is the FOURTH can't-fail test found in this project, and
it is the one guarding unbounded recursion. A test that proves the guard only when a
database happens to be up is not protection; it is the appearance of it.
— Fix: assert against something reached immediately after the guard and before any I/O —
record calls to `ClaudeCodeAdapter.identity` (or the config `load`) and assert it is never
touched. That holds regardless of whether any database is reachable.
— Cost if wrong: none; the assertion becomes strictly stronger and environment-independent.

Ruling 10 (Task 8, Important plan-mandated — FIX): `capture status` in cli.py reads
`capture_settings` with a raw `s.conn.execute(...)`, the only place outside the store layer
that touches a table directly. My brief specified it verbatim.
— Why fix rather than defer: this project's central architectural rule is that frontends
parse and format while all data access goes through the `Store` protocol. This is the first
crack in it, and the reviewer's point is the decisive one — `capture status` is exactly the
kind of command later status-style commands get copied from, so the violation propagates.
It also means a future schema change to `capture_settings` would miss this call site,
because nothing in the store layer knows it exists.
— Fix: add `Store.enabled_capture_projects(owner_id) -> list[str]` and call that.
— Cost if wrong: one more method on a protocol that already has eight capture methods.

CONTROLLER ERROR (recorded, no ruling — the outcome was fine, the risk was not):
I dispatched Task 7's fix (which I explicitly instructed to COMMENT OUT the recursion guard
in hook.py to demonstrate its test could fail) while Task 9's implementer was concurrently
editing that same file. Task 9's implementer reported finding its edits reverted and the
guard replaced with commented-out "TEMPORARILY DISABLED FOR VERIFICATION" lines it had not
written — that was Task 7's demonstration, mid-flight.
— Verified after the fact: both guards are intact in committed code (spawn_drain at
hook.py:85, session_end at hook.py:119), no disabled remnants anywhere in the file, and all
13 guard tests pass. Nothing shipped broken.
— The rule I broke: never run two implementers on the same file. I had been relying on
"disjoint files" to run implementers in parallel, and here they were not disjoint — worse,
one was deliberately mutating the very safety mechanism the other depended on. Had the
demonstration's restore failed, the recursion guard could have shipped commented out.
— Also observed: the shared Postgres container was stopped mid-task by Task 9's fail-soft
verification while Task 8's implementer needed it. Task 8 correctly restarted the EXISTING
container with `docker start remem-db-1` rather than `docker compose up`, per Ruling 1.

FINAL WHOLE-BRANCH REVIEW (opus, 14 commits): no Critical findings. Four Importants.
Merge recommended after a fix wave.

Ruling 11 (final review, Important 1 — FIX): `remem capture drain --job ID` was never
implemented. The spec and the plan's own CLI listing both specify it; the plan's Step 4 code
silently dropped it. Without it a job that hits the attempt cap is permanently dead — visible
in `capture status` and unactionable. Also makes `Store.get_capture_job` dead code today.
— Cost if wrong: one flag on an existing command.

Ruling 12 (final review, Important 2 — FIX, the subtlest defect in the branch): the whole
drain runs inside ONE uncommitted transaction, because `cli.py::capture_drain` uses
`_session()` and `open_session` connects without autocommit. A genuine database error puts
the connection in `InFailedSqlTransaction`; `_safe_finish` then swallows an exception on
every subsequent statement and Postgres turns the final COMMIT into a ROLLBACK without
raising. Reviewer reproduced it: the CLI printed "claimed 2, succeeded 1, entries written 1"
while zero entries existed and BOTH jobs sat back at `pending`, `attempts=0`, `error=NULL`.
— So Ruling 8's diagnostic promise is void for exactly the failure class it targeted, the
report lies to the user, and the attempt cap can never fire for this class because the
claim's `attempts+1` is rolled back too. A killed drain is likewise invisible: the row never
reached `running` on disk, so even stale-reclaim cannot see it.
— Root cause of the blind spot: `test_drain_does_not_raise_when_writing_an_entry_fails`
raises a Python RuntimeError from a wrapper object, which never touches the connection. It
is a real test of control flow and a non-test of the database scenario it is named for.
FIFTH instance of this pattern in the project.
— Fix: give the drain an autocommit connection (also ends a `--limit 10` drain holding row
locks across up to 10 x 180s of `claude` calls), plus a test that reproduces a REAL aborted
transaction rather than a Python exception.
— Cost if wrong: a job failing partway through its own entry writes leaves earlier entries
committed. They are deduped on retry, and durable-partial beats silent-total-loss.

Ruling 13 (final review, Important 3 — FIX): the spec requires an unparseable model response
to record "the first 500 characters of raw output" in the job's `error`. Nothing does; the
raw output is discarded, so the one diagnostic that would explain a misbehaving prompt is
missing. This is the main real-world failure mode of an LLM in the loop.
— Cost if wrong: `error` grows by up to 500 characters on genuinely broken output.

Ruling 14 (final review, Minor 1 — ACCEPTED as a recommendation, not fixed): the recursion
guard's one link — that `REMEM_CAPTURE_CHILD` reaches the hooks of the Claude Code session
`claude -p` starts — cannot be tested offline, since testing it means actually spawning
Claude. Reviewer confirmed the code is correct by inspection and that the guard holds at
every point it can be checked, but recommends one manual confirmation outside the suite.
— Ruling: do not fix; surface to the user as the one thing worth confirming by hand before
relying on capture unattended. Fabricating an offline test here would be theatre.

Fix wave: 3 commits 93dd4f4..b1be353, all three Importants applied, 294 passed
(controller-verified). Controller also confirmed: zero `conn.execute` in cli.py; autocommit
applied at exactly ONE call site (the drain command) and the capture service calls only
`remember`, never `supersede` — so the multi-statement atomicity autocommit would break is
not used on that path.

Parked (fix-wave implementer's own notes, none blocking):
- Autocommit changes partial-failure granularity: a job failing partway through its entries
  now leaves earlier ones persisted while recording `entries_written = 0`, so that column
  can under-report. Strictly better than the previous behaviour (which discarded the whole
  batch and recorded nothing), and dedup prevents duplicates on retry. A transaction per job
  would fix it but reintroduces lock-holding. Accepted.
- `claim_capture_job` returning None conflates "no such row" with "another drain holds it",
  so a genuine concurrent collision reports as `No capture job <id>`. Rare; known rough edge.
- The Item 2 test monkeypatches `PostgresStore.put_entry` — heavier than the suite's norm,
  but the implementer explains the alternative (configuring the connection in the test)
  would have made the test pass BEFORE the fix, i.e. worthless. Correct reasoning; accepted.

## Progress

Task 1: fix round 1/5 (2 addressed per Rulings 5 and 6; commits a2582e1..ba03ac8; 199 passed).
  Controller verified both directly: `origins=query.origins` present in _clamped, and the
  index test now asserts "where" plus "status = 'pending'".
Task 3: fix round 1/5 (CRITICAL addressed per Ruling 7; commit dc890ec; 229 passed).
  Re-review: ADDRESSED, no new breakage — verified the scan is first-VALID not
  first-parseable (the decoy case returns the real entry), `[]` still succeeds, and a
  non-empty all-invalid array still raises. Implementer honestly reported that only 2 of
  its 3 new tests failed pre-fix; the third passes either way and is kept as a regression.
Task 3: complete (commits ba03ac8..dc890ec, re-review clean)
Task 4: minor (deferred, FLAG TO FINAL REVIEW — guards a severe failure mode): no test
  asserts that `env=` is actually passed to the real `subprocess.run` call in `distill()`.
  The recursion guard is unit-tested only via `build_env` in isolation. Controller verified
  the wiring IS correct today (`env=build_env(os.environ)` at claude_cli.py:68), so this is
  a coverage gap, not a live bug — but the failure it guards is UNBOUNDED RECURSION, and a
  future edit that dropped or overwrote `env=` would pass every test. Third can't-fail test
  found in this project; the pattern is worth naming.
Task 4: minor (deferred): the two prompt tests use loose substring checks, so a prompt that
  dropped its "environment variable values" / "file contents" clauses would still pass.
  Inherited from the brief; the actual prompt text is fully compliant.
Task 4: complete (commits 7090399..58eaf9e, review clean — spec ✅, quality approved)

Task 6: minor (deferred): `_already_captured` runs one search per candidate entry, so a
  five-entry job issues five queries and re-queries state it already has. Correct, just
  wasteful; an indexed existence check would be the follow-up.
Task 6: minor (deferred, accepted at pre-flight): the dedup lookup is capped at 200
  captured entries per project. A project past that could admit a duplicate title.
Task 6: minor (deferred): one test assertion adds no real coverage. Reviewer flagged it;
  low value to chase given the two new tests added in the fix round.
Task 6: fix round 1/5 (CRITICAL addressed per Ruling 8; commit 9a91e81; 275 passed).
  Re-review: ADDRESSED, no new breakage — entire per-job body wrapped beneath the specific
  handlers, every finish routed through `_safe_finish`, both new tests confirmed failing
  pre-fix and covering the two distinct scenarios.
Task 6: complete (commits b800b78..9a91e81, re-review clean)
Task 7: fix round 1/5 (Important addressed per Ruling 9; commit cd475c7; 275 passed).
  Re-review: ADDRESSED, no new breakage — assertion is now environment-independent
  (`identity` is reached immediately after the guard and touches no I/O), hook.py confirmed
  untouched, and the guard-removed demonstration verified authentic. Original enqueue
  assertion retained alongside, so coverage widened rather than narrowed.
Task 7: complete (commits 566b970..cd475c7, re-review clean)
Task 8: fix round 1/5 (Important addressed per Ruling 10; commit 93dd4f4; 281 passed).
  Re-review: ADDRESSED, no new breakage. Controller independently confirmed ZERO remaining
  `conn.execute` calls in cli.py — the layering rule now holds with no exceptions.
Task 8: complete (commits 9a91e81..93dd4f4, re-review clean)
Task 9: minor (deferred): reviewer noted test-coverage overlap between the autodrain tests
  and existing hook tests. Not a defect.
Task 9: complete (commits cd475c7..553864b, review clean — spec ✅, quality approved)

ALL 9 TASKS COMPLETE. 281 tests passing (controller-verified). Both hooks fail-soft with
the database down; no orphan `capture drain` processes.

Task 5: minor (deferred, plan-mandated): `finish_capture_job` returns None, so a caller
  cannot distinguish "wrong owner" from "already finished" from the return value. Same
  class as an earlier ruling in this project that made `store.pin` return bool. The drain
  always passes the correct owner, so it is unreachable today. Reviewer confirmed the
  WHERE clause DOES constrain both id and owner_id — this is about the return contract,
  not a scoping hole.
Task 5: complete (commits dc890ec..b800b78, review clean — spec ✅, quality approved)

Task 1: complete (commits bed4280..ba03ac8, re-review clean — both findings ADDRESSED,
  no new breakage; re-reviewer enumerated every Query field to confirm `origins` was the
  only one missing from _clamped)
Task 2: complete (commits a2582e1..3ab18a2, review clean — spec ✅, quality approved,
  ZERO findings; filter confirmed present in both search paths at store.py:216 and :296)

