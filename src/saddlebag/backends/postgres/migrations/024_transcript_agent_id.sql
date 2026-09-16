-- Subagent transcripts. Claude Code writes, beside each session's own
-- `<session-id>.jsonl`, one `<session-id>/subagents/agent-<agent-id>.jsonl`
-- per subagent that session dispatched. Every line in one carries the
-- PARENT's `sessionId`, so `(owner_id, harness, session_id)` cannot tell a
-- subagent from its parent - storing one would overwrite the other.
--
-- They are worth the column. Measured 2026-09-16: 392 files, 131MB, and
-- the only copy - the parent transcript holds none of their lines.
--
-- NULL means a session's own transcript, so every row written before this
-- migration is already correct as it stands. `agent_id` alone is not an
-- identity: four agent ids on the machine this was written on repeat under
-- different parents with different content.
alter table transcripts add column agent_id text;

-- Postgres's generated name for 020's unnamed unique constraint, read from
-- the live database rather than guessed.
alter table transcripts
  drop constraint transcripts_owner_id_harness_session_id_key;

-- `nulls not distinct` is the load-bearing half. Under the default, two
-- rows with a NULL agent_id never conflict, so a session's own transcript
-- would silently stop being unique the moment this column appeared.
-- Named, unlike 020's, because `put_transcript` targets it by name.
alter table transcripts
  add constraint transcripts_identity
  unique nulls not distinct (owner_id, harness, session_id, agent_id);
