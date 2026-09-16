-- Subagent sidecars. Beside every `agent-<id>.jsonl` Claude Code writes an
-- `agent-<id>.meta.json` naming the subagent's type, description and model -
-- for some subagents the only record of what they were for, and gone with
-- the session directory like the transcript beside it.
--
-- On the subagent's own row: one small fact about one file, with the same
-- identity and owner. NULL means "not stored" - a session's own row, a
-- subagent with no sidecar, and every row written before this migration,
-- which is exactly what the import's re-read rule keys on to backfill them.
--
-- bytea, not jsonb, for the reason `content` is: it is source. jsonb
-- normalises key order and whitespace and drops duplicate keys, so the
-- stored copy would stop being the file.
alter table transcripts add column meta bytea;

-- A backfill that writes sidecars and no transcript bytes would otherwise
-- leave a run row indistinguishable from one that did nothing.
alter table transcript_runs
  add column metas_written int not null default 0;
