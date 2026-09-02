# Ingesting markdown docs into remem

Design, 2026-09-01.

## The problem

`docs/superpowers/` holds roughly 900KB of markdown across 22 files - the specs,
decision records and notes that CLAUDE.md describes as "the reasoning behind
rulings git history does not capture". None of it is in remem. An agent that
wants to know why `entry_events` carries no foreign key has to know which of
22 files to open, and `remem search` cannot help.

Ingest turns that corpus into searchable entries, at the granularity of the
passage that answers the question rather than the file that contains it.

## What gets built

One new service (`services/ingest.py`), one new CLI command (`remem ingest`),
two new `Origin` members, and a flag on `search`/`recall`. No new tables and
no migration.

## Decisions

### Chunk, do not summarise or point

A hit returns the passage that answered. The alternatives were one entry per
file (a 124KB body swamps any context it lands in, and embeds as a single
vector, so the semantic tier cannot tell "Task 9" from "Task 3") and a
pointer entry per file (trivial to build, but recall then surfaces a filename
and the agent still has to go read 124KB).

Chunking is what makes the semantic tier useful here. It is the reason to
build this at all.

### Split on headings, and only on headings

Heading density differs sharply by document type:

| | files | bytes | chunks | bytes per chunk |
|---|---|---|---|---|
| specs | 9 | 139KB | 167 | 833 B |
| notes | 2 | 28KB | 30 | 946 B |
| decisions | 2 | 54KB | 14 | 3.9KB |
| plans | 9 | 682KB | 111 | 6.1KB |

Measured 2026-09-01, headings counted outside code fences.

Specs and notes chunk naturally at well under a kilobyte. Plans are seven
times heavier per chunk, and their worst sections are far worse than the
average: `2026-08-26-remem.md` splits into 15KB sections such as `Task 9:
Session wiring and the CLI`. The temptation there is a size-based
sub-splitter. It was rejected: a plan section is `**Files:**`,
`**Interfaces:**`, then fenced code - one block in `2026-08-26-remem.md`
Task 9 runs 264 lines - so the bulk of the 565KB of plans is *source code
that now lives in `src/`*. Any size-based split cuts through the middle of a
fence and produces a chunk that begins mid-function with an unterminated
backtick.

So: split on `h1`-`h3` for every document type. A 15KB plan task stays one
chunk. `Task 9: Session wiring and the CLI` is a real semantic unit even at
15KB, and it is reachable only on request (see the next decision).

Bodies over 32KB are **truncated with a pointer to the file**, never split.
Nothing in the corpus is close to that today; the cap exists so that a future
document without headings cannot write an unbounded body.

The one piece of fence awareness in the whole design is a boolean toggled on
` ``` ` lines, so that a `# comment` inside a code block is not mistaken for
a heading. That is heading *detection*, not fence-aware splitting, and it is
the reason no fence state machine is needed anywhere else.

### Two origins, because the allowlist is the only hiding mechanism there is

Plans are ingested but held out of default results. The mechanism is forced
by what already exists: `search.DEFAULT_ORIGINS` is an allowlist, and its
comment records that an `exclude_origins` field on `Query` was considered and
declined - "at the cost of a second overlapping filter in the store's SQL for
one caller. Chosen deliberately; if a fourth origin appears, look here."

This is that fourth origin, and a fifth:

- `Origin.INGESTED` - specs, decisions, notes. Added to `DEFAULT_ORIGINS`.
- `Origin.ARCHIVED` - plans. Left out of it, reachable with `--archived`.

Both are excluded from `kb.resolve` with no change: it already filters to
`[HUMAN, AGENT]`.

Two origins where provenance strictly says one - both arrive by the same
ingest path - is the cost. It is the same double duty `HANDOFF` already does,
and close to the same reason: handoffs are hidden because "a project hands
off dozens of times and every one of them would otherwise sit on top of the
results".

The parallel is by volume, not by count, and it is worth being exact because
the first estimate of it was wrong in the other direction. Plans are the
*minority* of chunks - 111 against 211 of specs, notes and decisions - so
they cannot flood a result set by number. They dominate by size: 682KB
against 221KB, 6.1KB per chunk against 833 bytes. Twenty hits weighted toward
plans is several times more text than twenty hits of reasoning, and what that
text mostly contains is source code that now lives in `src/` and instructions
that have already been carried out.

If the corpus ever inverts - plans kept short, specs grown long - this
decision is the one to revisit.

The alternative, a tag exclusion filter, would build the mechanism the
`DEFAULT_ORIGINS` comment turned down and leave two hiding mechanisms to keep
in step.

### `--archive` is a flag, not a path heuristic

Which documents are archive is said at the call site:

```bash
remem ingest docs/superpowers/specs docs/superpowers/notes \
             docs/superpowers/decisions-*.md
remem ingest docs/superpowers/plans --archive
```

Sniffing `plans/` out of the path would hardcode remem's own documentation
layout into a service meant to work on any repository, and would make the
classification invisible where it is invoked.

### Identity is `(src, sec)` tags, following handoff

`write.supersede` takes an `entry_id` the caller already holds. A re-ingest
has no id - it has a path and a heading. Handoffs solved the same problem
with a tag convention plus supersede and no new table, so:

| field | value |
|---|---|
| `kind` | `DOC` |
| `origin` | `INGESTED` or `ARCHIVED` |
| `title` | `events-and-recall design § Invariants worth not breaking` |
| `body` | the section verbatim, capped at 32KB |
| `project` | `project.resolve_project`, as every other write |
| `tags` | `src:<path>`, `sec:<heading-slug>` |

`(src, sec)` is the identity key. No document-type tag: `--archive` already
separates plans by origin, and `src:` carries the directory for anything
finer. A `spec`/`plan` tag would need deriving from the path, which is the
heuristic the previous decision rejected.

### No content hash - compare bodies

To decide whether a chunk changed, fetch the live entry and compare `body`.
Identical means skip entirely: no write, no `updated_at` churn, and the
existing vector stays valid so `remem embed` has nothing to redo. A hash
would need somewhere to live and would answer the same question.

### The sweep, and the anchor entry it needs

A renamed or deleted heading leaves a live chunk with no counterpart in the
new ingest. Left alone it stays live and silently stale, returning alongside
its own replacement with nothing to say which is current.

So ingest sweeps: every live entry tagged `src:<path>` whose `sec:` was not
seen this run is superseded. Rename reads as delete-plus-add, and the old
chunk is kept rather than destroyed.

This needs something to supersede *to*, because `set_superseded` requires a
replacement id and "superseded by nothing" is not expressible. Every ingested
file therefore also gets an **anchor entry** - the `h1`, the lead paragraph
and the path, tagged `src:<path>` with no `sec:`. Orphans are superseded by
the anchor, which reads correctly: this section is gone, the document is
here.

The alternative was a `retired_at` column, which is honest about what
happened but costs a migration plus a new predicate that every query in the
store then carries, for one caller - the same trade `DEFAULT_ORIGINS`
declined.

The anchor pays for itself independently: it is the entry that answers "which
document covers events?" as opposed to "which paragraph".

## Architecture

`services/ingest.py`. Every decision below is policy and lives in the
service; the CLI passes paths and prints counts.

```
discover   paths -> **/*.md
parse      split on h1-h3, tracking fence state for heading detection only
diff       per chunk: absent          -> remember
                      body identical  -> skip
                      body differs    -> supersede
sweep      live entries tagged src:<path> with an unseen sec:
                                      -> supersede by the anchor
```

The sweep needs no new store method. `Query.text` is optional and the store
already has a listing branch for it, so a file's live chunks are
`Query(tags=['src:<path>'], include_superseded=False)`.

## Frontend surface

```
remem ingest <path>...          files or directories, globbed **/*.md
    --archive                   write as ARCHIVED instead of INGESTED
    --project / --global        as every other write
    --dry-run                   report the plan, write nothing

remem search --archived <q>     mirrors --handoff
```

MCP `recall` gains `include_archived`, mirroring the `include_handoffs`
parameter it already exposes.

`--dry-run` earns its place: this is the only thing in remem that rewrites
entries in bulk, and `12 new, 3 changed, 41 unchanged, 2 swept` before
anything is touched is worth having.

## Error handling

Fail-loud, like `remem embed` and unlike every hook. This is a command a
person typed.

Per-file failures collect rather than abort - one bad encoding in a
directory must not cost every other file in it. Failures are reported at the
end and exit 1; what worked is kept.

| case | behaviour |
|---|---|
| no headings in the file | anchor plus one chunk holding the whole file |
| `#` inside a fence | not a heading |
| undecodable bytes | skip the file, name it in the summary, exit 1 |
| >200 live chunks for one path | raise |

The last row is the one that most needs a comment in the source. The sweep
lists through `Query.limit`; sweeping a silently truncated list would
supersede live chunks at random. That is the worst failure available here, so
it fails loudly instead of guessing.

## Testing

The chunker is pure - no store, no I/O - so **the parsing tests carry no `db`
marker and run everywhere**. Given this project's history with `db` and
`opencode` markers hiding tests on CI, the split is deliberate: heading
detection against fences, heading-path construction, the 32KB cap and anchor
construction are all provable without Postgres.

`db`-marked, the round trip that is the feature:

```
ingest         -> n entries written, anchors present
re-ingest      -> 0 writes, updated_at unchanged      (the skip path)
edit a body    -> 1 supersede, old kept
rename heading -> new chunk plus orphan superseded by the anchor
```

Plus a visibility test mirroring `test_extract_origins.py` and
`test_handoff_visibility.py`: `INGESTED` in `DEFAULT_ORIGINS`, `ARCHIVED` out
of it, both absent from `kb.resolve`. That test is what stops a future origin
from silently vanishing in the way the `DEFAULT_ORIGINS` comment warns about.

## Out of scope

- Watching the filesystem. Ingest runs when invoked.
- Ingesting anything but markdown.
- Automatic ingest from a hook. The corpus changes on the order of once a
  week; a command is the right granularity.
