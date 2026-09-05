# Finding duplicate entries

Design, 2026-09-05.

## The problem

remem accumulates entries that say the same thing twice, and nothing in the
store can name them.

Two sources remain, now that a third is closed. The rename fix
(commit 733c4b9) removed the mechanical one: a memory file renamed on disk
used to mint a second entry holding the same body. What is left is not
mechanical and cannot be fixed at a write boundary:

- **The same knowledge written into two files.** Two memory files, or a
  memory file and a hand-written note, carrying one fact under different
  names. The `mem:<name>` mapping is sound; it is the human filing that
  duplicates.
- **The same knowledge written by hand and by a machine.** A rule written
  deliberately and an `EXTRACTED` entry the extractor produced from the
  session where that rule was discussed. Both are legitimate writes. Neither
  side can see the other at the moment it writes.

Neither is a bug to prevent. Both are a condition to observe, so the report
is the deliverable and merging stays a human decision.

Search cannot answer this question. Its three tiers rank entries against a
*query*; nothing ranks entries against *each other*, and the tiers
deliberately never blend, so even a perfect query would show one tier's
answer and hide the rest.

## What gets built

- `store.exact_duplicate_groups()` and `store.near_duplicate_pairs()`: two
  read-only queries, no migration.
- `services/dedupe.py`: every judgement - suppression, ranking, thresholds,
  coverage reporting - plus one write, `resolve()`.
- `remem dedupe report` and `remem dedupe resolve`.
- `DuplicateSet`, `NearPair`, `DedupeReport` in `domain.py`.

No migration. `entries` stores no body hash and needs none: `md5(btrim(body))`
computed in the `group by` is free at this store's size, and a stored hash
would be a second writer's problem for a report that has no writers.

## Decisions

### Both tiers always run, and never merge into one list

remem's search tiers run in a chain - each only when the one above returned
nothing - and that is correct for a lookup, where the caller wants one answer
and the tier says how much to trust it.

A report is the other question. It asks what duplication exists, so
suppressing the near tier because the exact tier found something would hide
most of the answer. Both tiers run over the whole population.

They still never blend. A pair reported as an exact group is not reported
again as a near pair, and the two tiers are rendered under separate headings
with the similarity score shown for the near half - the same reason every
frontend must surface `Hit.match`. An agent or a human handed an unmarked
approximate match treats it as certain.

### Exact groups are groups; near-duplicates are pairs, never chains

Identical checksums are transitive, so the exact tier's groups are real
equivalence classes and are rendered as such.

Similarity is not transitive. If A is near B and B is near C, A and C may
have nothing to do with each other, and a union-find over the pairs would
produce mega-groups whose membership no one could defend. The near tier
reports pairs and only pairs.

This is the same refusal the rename fix made when it declined to match on
titles or near-identical bodies: re-tagging on a coincidence is worse than
the duplicate it would have removed.

### The checksum is over the body alone, and trimmed

Two entries holding one body under different titles are duplicates. Hashing
title and body together would miss exactly the case a hand-written entry and
a machine-written one produce, since the extractor's title is never the one a
human would have chosen.

This is deliberately the opposite of `ingest`, which compares title **and**
body. That comparison answers a different question - whether a chunk needs
re-indexing - and there the title is half the embedding text and the
highest-weighted field in the tsvector, so a retitle must supersede. Nothing
is being re-indexed here.

`btrim`, because a hand-written memory file and a remem-generated one can
differ by a trailing newline, and that is not a different fact. Nothing
looser: normalising whitespace inside the body would start merging entries
whose formatting genuinely differs, and the near tier already exists to catch
those with a score attached.

### The near tier has its own threshold, not `semantic_threshold`

`config.semantic_threshold` is a search-recall floor: how loose a match is
still worth showing a caller who asked a question. Sameness is a different
bar, and a much higher one.

Reusing the search value would couple two unrelated knobs, so that tuning
recall silently retunes what counts as a duplicate. The default is `0.95`,
overridable with `--threshold`.

### The report never builds an embedder, and never renders unchecked as clean

Vectors are written only by `remem embed`, and the embedder is an optional
extra. So the near tier queries the vectors that exist and constructs
nothing: building a `LocalEmbedder` imports fastembed, builds an ONNX session
and can download ~130MB, which no report should ever do, and which would
fail outright where the extra is not installed.

The consequence is that the near tier's coverage is partial by default. The
report therefore always prints *embedded / total* for the population it
examined, and when nothing is embedded it says the near tier was **not
checked**, and why. It never prints an empty near section that reads like a
clean bill of health.

This follows `remem doctor`: unchecked never renders as `ok`, and "I could
not tell" does not exit non-zero. `remem embed` remains the fail-loud half,
because there an unavailable embedder is the whole job failing.

### `report` exits 0 even when it finds duplicates

Duplicates are the normal condition this command exists to describe, not an
error. Exiting non-zero on a finding would make the exit code mean "there is
something to read", which is every run, and an exit code that is always
non-zero is one people learn to ignore - the failure `doctor` already refuses
by exiting non-zero only for a missing *required* hook.

### `resolve` is a new verb, because `supersede` cannot express this

`remem supersede` requires `--title` and mints a **new** entry. It is for
knowledge that stopped being true: the replacement is written now, and the
old entry is kept as history.

Deduplication is the other operation. Both entries already exist and one of
them should point at the other. That primitive is `store.set_superseded`, and
it is already recognised here as distinct - the ingest orphan sweep calls it
directly rather than going through `write.supersede`, precisely because there
is no replacement to mint.

It gets its own verb rather than a `--with` mode on `supersede`, so that
`supersede`'s help text keeps stating one promise, and so the frontend needs
no mutually-exclusive-flag branch - which would be a policy decision in
`cli.py`.

`remem dedupe resolve <drop-id> --keep <keep-id>` is **fail-loud**, unlike
the report and unlike every hook in this repository, for the same reason
`remem ingest` and `remem handoff write` are: a person is standing there
having asked for it.

It refuses, with a message and a non-zero exit:

| refusal | why |
|---|---|
| `drop` and `keep` are the same id | an entry cannot supersede itself |
| `drop` is already superseded | its chain already has a head; re-pointing it rewrites history silently |
| `keep` is itself superseded | it would point a live entry at a tombstone |
| either id is not the caller's | already enforced inside the store as `NotOwner` |

### The suggested survivor is a suggestion

Each group and pair prints one runnable `resolve` line. The entry it proposes
keeping is chosen by highest-trust origin first (`HUMAN` and `AGENT` above
`EXTRACTED` and `INGESTED`), then most recently updated, then by id so the
output is deterministic across runs.

That ranking is a heuristic and is allowed to be wrong; it is printed as a
line to run, edit, or ignore. The alternative considered and rejected was an
interactive `--fix` that walks the groups: it would turn a diagnostic into a
destructive command, and this repository's rule is to rehearse destructive
automation against a copy first. Printing UUIDs the user must retype was also
rejected - that is the difference between a report acted on and one read
once.

### What `resolve` does to a memory file, stated rather than handled

Resolving an entry that carries a `mem:<name>` tag drops it out of its
collection, so the **next** `remem memory sync` sees a file whose entry is
gone and wants to delete it. That is correct: the knowledge now lives under
the surviving entry.

The two sync gates still hold - a file whose checksum does not match its
watermark is never deleted, so a copy edited by hand since the last sync
survives and is reported. No code is added for this interaction. It is
written down here because a user who resolves duplicates and then watches
files disappear should find it documented rather than surprising.

## Architecture

```
cli.py  dedupe_app: report, resolve          parse and format only
services/dedupe.py                           every judgement, and one write
store.py  exact_duplicate_groups()           Protocol
          near_duplicate_pairs()
backends/postgres/store.py                   the two queries
domain.py  DuplicateSet NearPair DedupeReport
```

Both store methods take a `Query` and reuse `_entry_filters`, so
`--project`, `--kind` and `--tag` narrow the population exactly as they narrow
search, and no filtering logic is written twice.

`exact_duplicate_groups` groups live entries by `md5(btrim(body))` having
`count(*) > 1`.

`near_duplicate_pairs` self-joins `entry_vectors` with `a.id < b.id` so each
pair is computed once, filters `1 - (a.vector <=> b.vector) >= threshold`,
orders by similarity, and returns the top `limit` pairs **together with the
total count above the threshold**. The count is what lets the report say
"showing 200 of 431" instead of quietly truncating - a report that names no
boundary is the diagnostic that eventually lies confidently.

`--limit` bounds the **near tier only**. The exact tier is unbounded: its
groups are a finite, cheap-to-compute fact about the store, and a truncated
list of identical bodies would be a report that hides the easiest half of its
own answer. The near tier needs a bound because its pair count grows with the
square of the population.

`DedupeReport` carries: the exact groups, the surviving near pairs, the pair
total before truncation, the threshold used, the model name, and the embedded
and total counts for coverage. Everything the renderer needs, so that
rendering takes no second trip to the store.

The population for both is live entries only (`superseded_by is null`), all
origins, all projects, for the calling owner. All origins by default because
duplication crosses those lines by construction: a hand-written rule and its
`EXTRACTED` twin is one of the two sources this command exists to find, and
an origin allowlist would hide it. This is the deliberate exception to
`search.DEFAULT_ORIGINS`, which is an allowlist for a different purpose.

The service is the only place that:

- drops a near pair whose members share an exact group,
- ranks the suggested survivor,
- decides that zero embedded entries means "not checked" rather than "none
  found",
- and applies the refusals in `resolve`.

Ranking, suppression and rendering are pure functions taking already-fetched
entries, so they are unit-testable without a database.

## Error handling

| failure | behaviour |
|---|---|
| no duplicates found | prints so, exit 0 |
| no vectors for the model | near tier reported as not checked, with the count and a pointer to `remem embed`, exit 0 |
| some entries unembedded | coverage line names how many were not compared, exit 0 |
| more pairs than `--limit` | prints "showing N of M", exit 0 |
| `resolve` refusal | message naming which rule, exit 1 |
| `resolve` on an id that is not the caller's | `NotOwner` from the store, exit 1 |
| database unreachable | `_session` raises `typer.Exit(1)`, as everywhere else |

## Testing

No `db` marker, running on CI:

- survivor ranking: origin beats recency; recency breaks an origin tie; id
  breaks a full tie, and the order is stable across runs.
- suppression: a pair whose members share an exact group is dropped; a pair
  where only one member is in a group is kept.
- rendering: the coverage line for zero, partial and full coverage, and the
  three spellings are distinct; the truncation line appears only when the
  total exceeds the limit.

`db`-marked:

- exact groups: identical bodies group; bodies differing only by a trailing
  newline group; bodies differing by internal whitespace do **not**; a
  superseded member is excluded; entries differing only in title still group.
- **a second principal holding byte-identical bodies never appears in any
  group or pair**, asserted by id. A fresh fixture guarantees the absence of
  other rows, which is exactly why the second principal has to be seeded.
- near pairs: vectors inserted **directly**, never by running the embedder -
  deterministic, no fastembed dependency, and the threshold behaviour is
  spelled with literal values rather than built from the code's own
  constants.
- near pairs: entries with no vector are absent rather than erroring; a pair
  is returned once, not twice.
- coverage counts with no vectors, some, and all.
- `resolve`: each of the four refusals, and the happy path, with the store
  read back to confirm `superseded_by`.

Every refusal in `resolve` is watched failing before it is trusted: revert
the guard, confirm the test goes red, restore it.

Any test whose path reaches `hookio.spawn_process` or `spawn_ingest` stubs
both. Nothing here should reach them, and the assertion is cheap.

## Out of scope

- Automatic merging, in any form. Never.
- An interactive `--fix`.
- A stored body hash, or an index on it. Revisit when the store is large
  enough that the `group by` is measurably slow, with the measurement in
  hand.
- Backfilling vectors from inside the report.
- Cross-owner or team-scope deduplication.
- Anything that changes what `remem supersede` means.
