-- Raw material, append-only. Everything downstream is derived from this and
-- recomputable from it; nothing here is derived from anything.

create type event_kind as enum ('tool_call','message','session_end');

create table events (
  id            uuid primary key,
  owner_id      uuid not null references principals(id),
  project       text not null,
  harness       text not null,          -- 'claude-code' | 'cursor' | 'opencode'
  session_id    text not null,
  kind          event_kind not null,
  tool          text,                   -- null for non-tool events
  payload       jsonb not null,
  occurred_at   timestamptz not null,
  recorded_at   timestamptz not null default clock_timestamp()
);

-- kind is small and closed; everything harness-specific stays in payload,
-- unparsed. A harness changing its payload shape must not be able to break
-- the write path - parsing is the extractor's problem, and the extractor can
-- be fixed and re-run because the raw is still here.
--
-- Payloads are stored IN FULL. Truncating at record time is lossy forever
-- and would degrade the extraction this table exists to enable. The
-- mitigation is the opt-in gate in services/record.py and an explicit prune
-- the user reaches for - not partial capture, and not automatic expiry.

-- The two queries that matter: a session's events in order (extraction), and
-- the newest event per session (the idle trigger).
create index events_session_idx
  on events (owner_id, project, harness, session_id, occurred_at);

create table entry_events (
  entry_id   uuid not null references entries(id) on delete cascade,
  event_id   uuid,                      -- deliberately no FK, see below
  session_id text not null,
  harness    text not null,
  primary key (entry_id, event_id)
);

-- NO FOREIGN KEY ON event_id, DELIBERATELY.
--
-- This row is an audit record, not a live relationship. "This entry came
-- from event X, in session Y, on harness Z" is a fact about the past, and it
-- stays true after event X is pruned. A foreign key would model it as though
-- it had stopped being true, which is the wrong claim - and would force
-- prune to choose between blocking and erasing provenance, both worse than a
-- pointer to something we deleted on purpose. session_id and harness are
-- denormalised here for the same reason: they are what survives the prune.
--
-- Two rules follow, and they are load-bearing:
--   1. NO READ PATH MAY DEREFERENCE event_id. The only query allowed to
--      follow it is forensic - show me the raw behind this entry, if we
--      still have it - and that query treats absence as an ordinary answer.
--   2. DANGLING MUST BE VISIBLE, NEVER INFERRED. prune reports how many
--      provenance rows it just left dangling, and the forensic lookup says
--      "event pruned" rather than "not found". Silence about deleted raw is
--      how a user concludes provenance was never recorded at all.
--
-- Ids are uuid7, so a dangling event_id cannot later be reused by a
-- different event and quietly acquire a wrong meaning.

create index entry_events_event_idx on entry_events (event_id);
