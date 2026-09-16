-- Derived from transcripts.content, and explicitly droppable: this table can
-- be deleted and rebuilt from the source at any time, which is what makes a
-- parser bug recoverable instead of fatal.
--
-- The rule that follows from that, and the one thing most likely to be
-- broken later: NOTHING MAY STORE ANYTHING ONLY HERE. Labels, chunks and
-- training signals reference (transcript_id, seq) from their own tables. The
-- moment a label lives in this table, rebuilding the parse destroys training
-- data and "derived lives apart from its source" inverts.
--
-- (transcript_id, seq) is a stable coordinate because JSONL line numbers do
-- not shift under append - which is precisely why the import refuses to
-- follow a file that shrank.
create table transcript_lines (
  transcript_id uuid not null references transcripts(id) on delete cascade,
  -- 0-based line number within the file.
  seq int not null,
  -- `type` and `occurred_at` are conveniences hoisted out of `raw` for
  -- indexing, and both are nullable on purpose. Claude Code's format is not
  -- ours and will change; a line whose shape is unrecognised still stores,
  -- with nulls, rather than failing the import. `raw` is always complete.
  type text,
  uuid text,
  occurred_at timestamptz,
  raw jsonb not null,
  primary key (transcript_id, seq)
);

create index transcript_lines_type_idx on transcript_lines (transcript_id, type);
