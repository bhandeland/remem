# Capturing subagent sidecars

Design, 2026-09-16. Closes the open decision recorded at the end of
[Capturing subagent transcripts](2026-09-16-subagent-transcripts-design.md).

## The decision

Store them. Beside every `agent-<id>.jsonl` Claude Code writes an
`agent-<id>.meta.json` naming the subagent's `agentType`, `description`,
`spawnDepth` and usually its `model`. For some subagents it is the only
record of what they were for - the transcript opens with the prompt, not
with the role that was dispatched - and it is as irreplaceable as the
transcript beside it: once Claude Code deletes the session directory, it is
gone.

Measured 2026-09-16 across the whole transcript root, listing every name in
every `subagents/` directory rather than filtering for the expected ones:

- Two kinds of file and nothing else: 433 `.jsonl`, 432 `.meta.json`.
- Every sidecar has its transcript beside it. One transcript has no sidecar.
- Every sidecar parses as a JSON object. Sizes run 139 to 335 bytes.
- **Write-once.** No sidecar was modified more than a second after it was
  created (`st_mtime - st_birthtime`, macOS). The re-read rule below leans
  on this.

## Data model: migration 025

```sql
alter table transcripts add column meta bytea;
alter table transcript_runs add column metas_written int not null default 0;
```

- `meta` lives **on the subagent's own row**, not in a table of its own:
  it is one small fact about one file, with the same identity and the same
  owner. NULL means "not stored", which covers a session's own row (which
  has no sidecar), a subagent with none, and every row imported before 025.
- **`bytea`, not `jsonb`.** It is source, and the source rule is raw bytes:
  `jsonb` normalises key order and whitespace and drops duplicate keys, so
  the stored copy would stop being the file. Anything that wants to query
  it can cast; nothing derived is stored.
- `metas_written` on the run row, because a backfill of 244 sidecars that
  moved no transcript bytes would otherwise record a run indistinguishable
  from one that did nothing.
- `Transcript` gains `has_meta: bool` (`meta is not null`), never the bytes,
  for the reason `content` is absent.
- The store gains `set_transcript_meta(id, owner, meta) -> bool` (False when
  the ownership guard matched nothing, as `append_transcript`) and
  `transcript_meta(id, owner) -> bytes | None`.

## Enumeration

`TranscriptFile` gains `meta: Path | None`. `transcript_files()` stays the
only code that knows the layout: it lists the sidecars once per directory
with a glob and pairs each by name, so it still stats nothing. A sidecar
with no transcript is not returned - none exists today, and the identity it
would need comes from the transcript's row.

## The re-read rule

After a subagent file's transcript has been handled - whatever its plan,
`SKIP` and `SHRUNK` included - the import stores its sidecar when **the
file has one and the row has none**. Nothing else triggers a read.

- That one rule is both the backfill and the steady state: the 244 rows
  imported before 025 all classify `SKIP` on their transcript, and would
  never pick a sidecar up if the check lived inside the new/append paths.
- **Never overwritten.** A stored sidecar is not re-read, so there is no
  change detection - the measurement says there are no changes to detect.
  If Claude Code starts rewriting them, this is the rule to revisit.
- **Never cleared.** A sidecar that disappears from disk leaves the stored
  copy alone, like a shrunk transcript.
- **Validated, then stored raw.** Bytes that do not parse as a JSON object
  are not stored and are reported as a failure. With never-overwrite, a
  sidecar caught mid-write would otherwise be kept torn forever; refused, it
  is simply tried again next run.
- **Does not spend the refresh cap.** The cap bounds transcript I/O, and a
  sidecar is at most a few hundred bytes. Spending it would make the
  backfill take ten session starts to read 70KB.
- A row whose store call failed this run has no id to attach to, so its
  sidecar waits for the run that stores the transcript.

## Status

`TranscriptStatus` gains `meta_backlog`: subagent rows that are stored,
have no sidecar stored, and have one on disk. A subagent file that is not
stored at all is already in `subagent_backlog` and is not counted twice.
`status_to_dict` carries it in every state; the CLI prints it on the
backlog line, and `import` prints `metas_written` beside its other counts.

No advisory: like the backlog, it is normal between bounded runs.

## Testing

- `transcript_files()` pairs a sidecar with its transcript and leaves a
  transcript without one as `None` - no `db` marker.
- Store (`db`): `set_transcript_meta` round-trips bytes, refuses another
  owner, and `has_meta` follows it; migration 025 leaves existing rows NULL.
- Import (`db`): a new subagent stores its sidecar byte-exact; a row
  imported before its sidecar existed picks it up on an otherwise-SKIP run
  and counts it in `metas_written`; a stored sidecar is not re-read after
  the file changes; a deleted sidecar leaves the stored copy; an invalid
  sidecar is a failure and stores nothing; a capped run still stores
  sidecars for files it did not read.
- Status (`db`): `meta_backlog` counts only stored rows lacking one.
- Each guard watched failing on a scratch copy, `__pycache__` cleared
  between variants.

## Rollout

1. `pg_dump` the transcript tables to the scratchpad.
2. `bag db status`, `bag db up`.
3. `bag transcripts import --project saddlebag`. Expected: 0 new
   transcripts, 243 sidecars written (the -remem count), no failures.
4. Verify: `meta_backlog` 0; stored sidecar bytes total equals the on-disk
   total for that directory; spot-check sidecars byte-identical.

## What this does not do

- No capture of `tool-results/`.
- No rendering of the sidecar anywhere, and no derived columns from it.
