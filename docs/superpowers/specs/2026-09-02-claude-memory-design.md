# Claude Code memory: one source of truth

Design, 2026-09-02.

## The problem

Claude Code keeps a file-based memory per project at
`~/.claude/projects/<cwd-slug>/memory/` - one `MEMORY.md` index loaded into
context every session, plus one file per fact, surfaced individually by
relevance in `system-reminder` blocks. There are 13 such directories on this
machine, 38 fact files, roughly 90KB.

remem keeps the same kind of knowledge in Postgres and injects it at
`SessionStart`. The two stores have already drifted into duplication:
`cursor-runs-claude-code-hooks.md` and `remem-fail-soft-hides-budget-failures.md`
exist in both, written twice, free to disagree.

The duplication is the symptom. The structural fact is that both stores answer
the same question - what should this session know before it starts - through two
mechanisms that overlap almost completely. Two writers, no reconciliation.

The goal is one source of truth: a fact is written once and appears in both
places.

## Ruling

remem owns the directory. It is generated from the store, the way opencode's
`remem.js` and cursor's `remem.mdc` are generated. But unlike those two, this
one has a second writer that cannot be told to stop - Claude Code writes
memories there unprompted, mid-session, and those writes are real knowledge.
So the sync **adopts before it regenerates**: anything on disk that remem has
not seen becomes an entry first, and only then is the directory rewritten from
the store.

Selection is by a **designated collection**, one per project, opt-in. An
undesignated project generates nothing.

## Identity and field mapping

One file, one entry, one tag: `mem:<name>`, from the frontmatter `name:` slug.
That slug is already the file's stable id - it survives a rename of the file
and it is what `[[wiki-links]]` resolve against. Same shape as `topic:<slug>`
for handoffs and `src:`/`sec:` for ingest. On export the filename is derived
from the slug, so there is one identity and no way for two to disagree.

A memory file carries three strings where `Entry` had two. The `MEMORY.md` link
text, the frontmatter `description:` and the body are all distinct, and
`Entry` has only `title` and `body`. Deriving the description from the body's
first sentence was considered and rejected: it is lossy in the one way that
matters here, because every export would rewrite it, the file's checksum would
never match its watermark, and the "file changed" case would fire forever on a
file nobody touched. `Entry` therefore gains a nullable `summary: str | None`.
Nothing else reads it, so no existing behaviour moves.

| memory file | remem entry |
|---|---|
| `name:` | `mem:<name>` tag |
| `MEMORY.md` link text | `Entry.title` |
| `description:` | `Entry.summary` |
| `metadata.type:` | `type:<user\|feedback\|project\|reference>` tag |
| body | `Entry.body` |
| `[[link]]` | left as literal text |

`kind` is `note` and `origin` is `agent` on adopt. An agent wrote it, which is
precisely true, and it avoids adding an `Origin` - see Invariants.

`[[link]]` stays literal. It is already a slug that round-trips to `mem:<slug>`,
so resolving it into an entry id at rest would create a second identity to keep
in step with the first.

## Which side moved

Body comparison alone cannot answer this. It says the two differ, not who
changed. So the sync keeps a watermark: `.remem-sync.json` in the memory
directory, mapping each name to `{entry_id, body_sha, exported_at}`.
Dot-prefixed, so Claude does not index it as a memory.

`body_sha` is the sha256 of the **body text alone** - everything after the
closing frontmatter delimiter, byte for byte as it would be written - and the
entry side hashes `Entry.body` the same way. Frontmatter is excluded on
purpose: `title`, `summary` and the type tag are compared as values, and
folding them into the hash would make a whitespace difference in generated YAML
read as a content change. `exported_at` is never compared; it is shown by
`remem memory status` so a stale directory is visible, and comparing it would
reintroduce the clocks this scheme exists to avoid.

Every case is then exact and needs no clocks and no mtimes:

- file sha != recorded - the file moved
- entry sha != recorded - the entry moved
- neither - in sync, no write
- both, and the contents differ - a real conflict

This is deliberately the opposite of `ingest`, which answers "did this change"
by comparing bodies and explicitly declines to store a hash. The reason for the
difference is that ingest's question is one-sided: a file is the only writer
there, so "differs" and "the file changed" are the same statement. Here both
sides write, and a recorded watermark is the only thing that makes "which one"
answerable at all.

## The six cases

Applied in this order by `remem memory sync`:

1. **File, no entry, no watermark** - a stray Claude wrote. Adopt as a new
   entry, add it to the memory collection.
2. **File and entry, no watermark row** - the watermark was lost, or the file
   was hand-created with a name that collides with an existing entry's tag. If
   the bodies are equal, heal: record the watermark and write nothing. If they
   differ, it is a conflict (case 5), because with no watermark there is
   nothing to say which side moved.
3. **File moved, entry did not** - adopt the edit via `write.supersede`, which
   carries the old entry's tags onto the replacement and so preserves
   `mem:<name>`. That tag-copying behaviour is load-bearing here exactly as it
   is for ingest.
4. **Entry moved, file did not** - regenerate the file.
5. **Both moved** - conflict. Fail loud, name the file, write the store's
   version alongside as `<name>.remem-conflict.md`, change nothing else.
6. **Entry gone from the collection** (superseded, unpinned, or no longer
   matching the query) - delete the file and its watermark row.

Two gates, and they are the whole safety story:

- A file whose sha does not match its watermark is **never deleted**, only
  reported.
- A file that changed on both sides is **never overwritten**.

remem removes and rewrites only what it wrote and knows to be untouched.
Anything else it hands back to the user.

## Selection, opt-in, and the budget

Memory export needs a value rather than a boolean - which collection - so it
does not reuse recording's gate but follows its shape. `Store` gains
`set_memory_collection(owner_id, project, slug | None)` and
`memory_collection(owner_id, project) -> str | None`. `None` means not
designated, which means `remem memory sync` does nothing and writes no file.
The gate is checked in `services/memory.py`, where every frontend gets it free.

Two alternatives were rejected. A naming convention (`<project>-memory` is the
memory collection) needs no migration but makes the designation invisible and
unqueryable - you would learn you had designated something by watching files
appear. `config.toml` is worse: that file is global where this is per-project,
and `services/settings.py` is deliberately the one service that opens no
database.

### The path is not the project

The memory directory is keyed by Claude Code's slug of the **absolute working
directory** (`-Users-brandon-llmworkspace-remem`), while remem's `project` is a
name resolved from the git common dir (`remem`). Neither is derivable from the
other, and the same project has a different slug in a worktree - which is the
ordinary case here, since this repo's own development happens in one. So the
capability takes a path:

```
memory_dir(self, cwd: Path) -> Path | None
```

The service holds both: `cwd` to find the directory, `project` to find the
designation and to file adopted entries.

### The budget

`MEMORY.md` is loaded every session and so is remem's `SessionStart` block,
which is 22.7KB in this project today. Exporting the wrong set means paying for
the same text twice and risking `RulesExceedBudget`, which dies silently on
every harness.

Designation is what keeps that from happening by accident: undesignated
projects generate nothing, so today's behaviour is unchanged everywhere until a
project is opted in. When one is, the memory collection and the KB collection
are different collections by default, and nothing prevents pointing both at the
same query - so `remem memory status` reports the overlap as a count and a byte
total. Not an error: overlapping deliberately is a legitimate choice, and the
job here is to make sure it is a choice rather than a surprise.

Recommended collection contents, as guidance rather than code: a query of
`kinds=[rule, note]` and `project=<project>`, no tag filter, plus explicit pins.
That excludes the 330 ingested chunks (kind `doc`) without hardcoding an origin
filter, and pins are the escape hatch for promoting a single extracted entry -
the move `remem kb pin` already exists for.

## Architecture

Downward-only, following the seams already here.

```
frontend:   cli.py            remem memory designate | sync | status
service:    services/memory.py   gate, six cases, watermark, sweep, overlap
adapter:    agents/claude_code/memory.py   memory_dir(cwd)
pure:       memory_file.py    parse / render a file, render MEMORY.md
store:      store.py + backends/postgres   designation, summary, migration 013
```

- **`memory_file.py`** is pure - no I/O, no store. Same standing as
  `markdown.py` and `session_size.py`, so its tests carry no `db` marker and
  run on CI, which is where a format bug should be caught.
- **`services/memory.py`** holds every policy decision. It is the only module
  that knows what a conflict is.
- **`agents/claude_code/memory.py`** knows the `/`-to-`-` slug of an absolute
  path, the `~/.claude/projects/<slug>/memory/` layout, and `CLAUDE_CONFIG_DIR`.
  Facts about Claude Code, kept on the adapter, exactly as `env_vars.py` is.
- **`agents/base.py`**: `memory_dir` becomes the **seventh** optional
  capability in that comment block, probed with `getattr`, warn-and-continue if
  it raises. CLAUDE.md's count of six changes in the same commit.

### Frontend surface

```
remem memory designate <collection-slug>    set the designation for this project
remem memory designate --none               clear it
remem memory sync [--dry-run]               the six cases
remem memory status                         designation, counts, conflicts, overlap
```

`designate` requires the collection to already exist and fails naming it
otherwise, rather than creating one. A collection created as a side effect of
designation would have an empty `CollectionQuery`, and an empty query matches
nothing forever - so the friendly version of this command silently guarantees
that no memory is ever exported. `kb create` already says so through
`kb.advisories()`; this command does not get to bypass it.

The CLI decides nothing else: it resolves `cwd` and `--project` and prints
counts.

There is no separate `adopt` command. Adoption is case 1 of `sync`, and a
second entry point would be a second place for the gate to be checked or
forgotten. The one-time import of the existing directories is therefore:
create a collection, designate it, `sync --dry-run`, `sync`. The gate makes
that ordering mandatory - you cannot adopt into a project you have not
designated - and that is deliberate.

### One convention exception

The generated `MEMORY.md` uses `— ` between link and hook, not remem's spaced
hyphen, because that line's format belongs to Claude Code and matching the
corpus already on disk matters more than matching remem's prose style. It is
the only place in the repo where that is true, and it carries a comment saying
so.

## Error handling

Fail-loud, like `ingest`, `embed` and `handoff write`, and for the same reason:
a person typed it. Per-file failures collect rather than abort, so one file with
unparseable frontmatter does not cost the other thirty-seven. A run prints a
per-case count, then the failures, then exits non-zero if any file was left in
conflict.

That exit code is a definite statement that work was not done, not an "I could
not tell" - which is the case `doctor`'s rule about exit codes explicitly
carves out as legitimate.

## Not in `remem doctor`

The designation lives in the database, and `doctor` reads files and opens no
connection - a diagnostic that needs the system healthy is no use when it is
not. So doctor stays out of this entirely. The line is clean anyway: doctor
asks whether the harness will ever call remem, and `memory sync` is not a hook.
The overlap count lives only in `remem memory status`.

## Testing

- `tests/test_memory_file.py` - no marker, runs on CI. Round-trip properties
  both directions, and the strongest test in the design: a vendored fixture
  corpus copied from the real memory files, asserting `render(parse(f)) == f`
  **byte for byte**. If that holds, "in sync, no write" is real rather than
  aspirational and the watermark cannot churn.
- `tests/test_memory_service.py` - marked `db`. One test per case, named for
  the case, plus a double-sync test asserting the second run writes nothing at
  all. That is the property everything else rests on.
- `tests/test_memory_capability.py` - no marker. Slug computation,
  `CLAUDE_CONFIG_DIR`, and a worktree `cwd`.

A test asserting that `base.py`'s "six capabilities" comment matches the number
actually probed was considered and declined: it would have to parse a prose
comment for a numeral, and a brittle guard on documentation is worse than the
documentation. The commit updating both is the control.

## Invariants worth not breaking

- **No new `Origin`.** Adopted memories are `Origin.AGENT` with a `mem:<name>`
  tag. Selection is by collection, so origin is not load-bearing for it, and
  this avoids the enum-migration hazard entirely: `entry_origin` is a Postgres
  ENUM, so adding a value always needs a migration, and a value added by
  `alter type ... add value` cannot be used in the transaction that added it.
- **The watermark is the only thing that answers "which side moved."** Delete
  `.remem-sync.json` and every file whose body still matches its entry heals
  silently, while every file that differs becomes a conflict needing a hand -
  which is the safe direction, and the reason case 2 exists rather than
  defaulting a watermark-less file to adopt.
- **`write.supersede` copies tags onto the replacement**, which is what makes
  `mem:<name>` survive an edit. Load-bearing, not incidental - the same
  property ingest depends on.
- **Case 6 uses the sweep discipline, not a bare delete.** A file is removed
  only when its sha matches its watermark.
- **`Entry.summary` is nullable and unread elsewhere.** If something later
  starts rendering it, the round-trip fidelity argument above has to be
  re-checked, because a second writer of that field reintroduces the churn
  problem it was added to prevent.

## Out of scope

- Running sync from a hook. Manual and idempotent first, matching `ingest`'s
  ruling. `hookio.spawn_process` is where it would go if manual sync chafes,
  and adding it later is a few lines - but doing it in that order means never
  debugging a silent adopt, since hooks are fail-soft and this command is not.
- Watching the filesystem.
- Any harness but Claude Code. `memory_dir` is a probed capability precisely so
  a second implementer is possible without this design being about it.
- Reconciling `[[links]]` into entry links.
- Deduplicating the facts that are currently in both stores. The first sync
  adopts the directory's copy; merging it with the pre-existing remem entry is
  a manual editorial pass, not something to automate.
