# Importing claude-mem into remem

Design, 2026-09-09.

## The problem

The knowledge worth keeping is on a second machine, in a tool that is being
decommissioned. claude-mem stores it in its own sqlite database under
`~/.claude-mem`; remem has no way to read it, and there is no shared format
between them.

Two facts shape everything below.

**remem will be installed on that machine.** This was the first thing worth
settling, because the alternative designs are much worse: copying a sqlite
file between hosts, or reaching across the network into a second database.
Installing remem where the data already is deletes the transport problem
entirely - the importer opens a local file, and no credential, no network
path and no partially-copied database ever exists.

**The source machine's claude-mem version is unknown.** Its schema is at
version 33, which unified everything into one `memory_items` table. The
snapshot available here for reference (exported 2026-06-30, in an Obsidian
vault) is the older shape: separate `observations`, `session_summaries` and
`user_prompts` tables. The importer must read both, and must decide which by
inspecting `sqlite_master` rather than by trusting a version number it cannot
verify.

## What gets built

- Migration `019_imported_origin.sql`, adding `imported` to the
  `entry_origin` enum, and that value added to `search.DEFAULT_ORIGINS`.
- `importers/claude_mem.py` - sqlite in, `SourceRecord` dataclasses out. No
  store access, no policy, no I/O beyond the one file it is given.
- `services/import_.py` - every policy decision: mapping, identity,
  idempotency, project resolution, dry-run.
- `remem import claude-mem <path>` in `cli.py`, with `--dry-run` and
  `--project`.

## Layering

The seam is the one this repo already enforces. `cli.py` parses and formats.
`services/import_.py` decides. `importers/claude_mem.py` is a pure reader:
given a path, it yields neutral records, and it never learns what an `Entry`
is. The Obsidian importer that follows this one is a second module against
the same `SourceRecord`, and that is the entire reason the record type is
neutral rather than shaped like claude-mem's rows.

Deliberately **not** an entry-point plugin seam like `agents/`. That seam
exists because third parties genuinely write harness adapters, and CLAUDE.md's
own defence of it is that the second adapter proved it by looking nothing like
the first. Nobody outside this repo is going to write an importer. A seam
bought before its second customer is speculation, and it can be added later
without changing a single mapping decision.

Also deliberately not routed through `ingest`. Ingest identity is `src:` and
`sec:` - a file path and a heading slug - and its "has this changed?" test
compares title and body. A claude-mem row has a stable id and no file at all.
Forcing rows through a path-shaped identity would mean inventing fake paths,
and the reuse would fight the model rather than serve it.

## The new origin

`Origin.IMPORTED`, added to the `entry_origin` Postgres enum by migration 019
and to `search.DEFAULT_ORIGINS`.

`DEFAULT_ORIGINS` is an allowlist, and CLAUDE.md states the invariant three
separate times: a new origin that is not added to it silently vanishes from
search. Adding it is therefore part of this migration's job, not a follow-up.

Imported knowledge gets its own origin rather than borrowing `EXTRACTED`
because the two answer different questions. `EXTRACTED` means remem's own
extractor wrote this from events it recorded, and it is excluded from context
blocks so machine text cannot crowd out hand-written rules. Imported entries
are machine-written too, but they came from a tool whose judgement remem
cannot vouch for and whose store is going away. Keeping them distinguishable
is what makes a bad import auditable and reversible; collapsing them into
`EXTRACTED` would make a bad import indistinguishable from remem's own work,
forever.

Imported entries are excluded from context blocks for the same reason
`EXTRACTED` is, by the same `kb.resolve` origin filter. They appear in
`search` and `recall`, and `remem kb pin` promotes one that earns it.

**One migration gotcha, designed around.** PostgreSQL will not let a new enum
value be *used* in the transaction that adds it. `remem db up` and `remem
import` are separate invocations, so this never arises in practice - but the
importer must fail loudly and name `remem db up` if it finds a schema without
`imported`, rather than silently falling back to `agent`. A test asserts the
refusal.

## Record mapping

### Observations -> kind=note

| claude-mem | remem |
|---|---|
| `title` | `title` |
| `subtitle` | `summary` |
| `narrative`, `facts[]`, `concepts[]` | `body`, rendered as prose |
| `type` (discovery/change/feature/bugfix) | tag `cmem-type:<type>` |
| `id` | tag `cmem:<id>` |
| `project` | remem project |

`subtitle` maps to `summary` because that is what it already is: a one-line
hook, written to sit under a title. remem's summary field feeds the context
block's short form and a memory file's frontmatter `description`, and having
one for free is worth more than any rendering of it into the body.

`facts` and `concepts` are JSON arrays of sentences. They are rendered into
the body as prose, never into tags: tags are the highest-weighted field in the
tsvector after the title, so a paragraph in a tag distorts ranking for every
query that happens to share a word with it.

### Session summaries -> kind=doc, origin=imported

Not `origin=handoff`, despite the shape being nearly identical - `request`,
`investigated`, `learned`, `completed`, `next_steps` maps almost field for
field onto a handoff's Done and Next steps.

The handoff origin carries invariants this data would break. Writing a handoff
supersedes the prior live one for the same `(project, topic)`, and handoffs
are excluded from search unless `include_handoffs=True`. Importing fifteen
historical summaries as handoffs would chain fourteen of them into superseded
history behind one live entry and hide all of them from ordinary search. As
`doc` with `origin=imported` each keeps its own identity and stays findable,
which is the only reason to import them at all.

### User prompts -> one entry per session

One entry per prompt was considered and rejected. In the reference snapshot
the prompts are strings like `/claude-mem:learn-codebase` - seventeen of them,
individually not knowledge, and seventeen near-empty entries would compete in
search results against real memories forever.

Grouped per session under `cmem:prompts:<session_id>`, the body is the
session's prompts in order, which is a record of what was asked. Same data,
no search pollution.

## Identity and re-import

Every imported entry carries `cmem:<source_id>`. That tag is what makes the
import re-runnable, and it plays exactly the role `src:`/`sec:` plays for
ingest: on a second run, an entry whose tag is already present and whose body
is unchanged is skipped without a write, and one whose body changed is
superseded.

Re-runnability is not a nicety here. The import will be run once as a dry run,
once for real, and quite possibly again after a mapping is corrected.

**There is no orphan sweep.** Ingest supersedes chunks that vanished from a
file because ingest owns those chunks and the file is the truth. Here the
source is being decommissioned: a row deleted from claude-mem - or a whole
database that stops existing - must never delete anything from remem. This is
a migration, not a sync, and the asymmetry is deliberate.

## How entries are written

Through `services/write.remember`, which already takes `origin`, `summary`,
`tags` and `project` - the importer needs no new write primitive. Its
`RuleNeedsSummary` check does not fire here: it is gated on `Kind.RULE`, and
nothing imported is a rule.

A changed record goes through `write.supersede`, **not** `store.set_superseded`.
This is the opposite of ingest's orphan sweep, which calls the store directly
because a heading deleted from a file has no replacement to point at. Here the
replacement is precisely what we have - the new version of the row - so the
primitive that mints one is the right one.

`IMPORTED` is added to `DEFAULT_ORIGINS` and deliberately **not** to
`INJECTED_ORIGINS`. Verified against the code: `kb.resolve` filters the query
half to `INJECTED_ORIGINS`, so an origin absent from that list is excluded
from context blocks without any further work. The one documented hole is
unchanged and acceptable - `store.pinned_entries` has no origin filter, so
`remem kb pin` can still promote an imported entry into a block, which is the
documented way to promote machine-written knowledge.

## Projects

claude-mem's project is a slug (`at-workspace`); remem resolves projects from
the git root. Neither derives the other, so the mapping is stated rather than
guessed:

- By default the claude-mem project name is used verbatim.
- `--project X` forces every record into one project.
- The dry run lists the distinct source projects it found, with counts,
  before anything is written. Discovering that an import is about to mint six
  projects is worth exactly one screen of output.

## Failure and output

Fail-loud, like `ingest` and `embed` and unlike every hook in this repo: a
person typed this and is watching it.

- `--dry-run` writes nothing and prints per-kind and per-project counts.
- An unreadable or non-claude-mem sqlite file is refused by name.
- A schema with neither `memory_items` nor the legacy tables is refused, and
  the message says which tables were looked for.
- A schema predating migration 019 is refused, naming `remem db up`.

**No embedder is constructed.** Building a `LocalEmbedder` imports fastembed
and can download ~130MB, and an import does not need one - entries land
without vectors and the report points at `remem embed`. This is the policy
`services.search.shared_embedder` and `embed.backfill_if_pending` already
apply everywhere else.

**No `import_runs` table.** `ingest_runs` and `memory_runs` exist because
those run unattended from hooks, where nobody sees the output. An import is
typed by a person reading the result, and a table recording a one-time
migration would be a schema kept forever for a fact used once.

## Testing

`importers/claude_mem.py` is pure - a sqlite file in, records out - so its
tests build a database with `sqlite3` in a `tmp_path` and carry no `db`
marker. They run on CI. Both schema shapes get a fixture, and the legacy
fixture is built from the real 2026-06-30 export's column set rather than from
the current schema's, so that it pins the shape the code actually has to
survive.

Per this repo's rule, fixture values are spelled literally rather than derived
from the code's own constants: a test that builds its expectations out of the
module under test pins nothing.

The mapping tests are pure too. Only the idempotency tests - skip unchanged,
supersede changed - need a store, and they carry `db`.

Every guard gets watched failing before it is trusted, with `__pycache__`
cleared between variants.

## What this design does not cover

The Obsidian importer, which is a second `SourceRecord` producer and a
separate spec, and the session-start banner, which shares nothing with either.
