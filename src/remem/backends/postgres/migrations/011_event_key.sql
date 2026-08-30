-- Deduplicate events by the harness's OWN event id.
--
-- Recording is a single INSERT from a fail-soft hook, so the same event
-- arriving twice is an ordinary failure, not an exotic one: a hook
-- registered twice under two command names (which happened, to both the
-- claude-code and cursor adapters), a harness retrying, a session recorded
-- from two places at once. Nothing downstream notices - the extractor just
-- reads the event twice.
--
-- The key is the harness's id and nothing remem computes. Payload equality
-- was considered and rejected in both directions: it MISSES real duplicates
-- (every payload carries a duration_ms/duration that differs between two
-- recordings of the same event) and it DROPS legitimate ones (running the
-- same command twice in a session is ordinary). A key that is wrong in both
-- directions is worse than no key.
--
-- tool_use_id is preferred over generation_id rather than combined with it:
-- every tool call in a generation carries the same generation_id and is
-- distinguished only by its tool id.
--
-- generation_id alone is NOT an event id - it identifies a generation, and
-- cursor's beforeSubmitPrompt and afterAgentResponse both carry the one
-- belonging to the turn they bracket. Keying on it without the hook name
-- would silently discard every agent response cursor records; three such
-- collisions were already present in a live database when this was written.
alter table events add column event_key text
  generated always as (
    case
      when payload->>'tool_use_id' is not null
        then 'tool:' || (payload->>'tool_use_id')
      when payload->>'generation_id' is not null
        then 'gen:' || (payload->>'generation_id')
             || ':' || coalesce(payload->>'hook_event_name', '')
    end
  ) stored;

-- Partial, because most events have no harness id to key on: claude-code's
-- SessionEnd payload carries none, and neither does opencode's message.
-- Those stay exactly as uncovered as they were before this migration -
-- visibly so, rather than under a constraint that pretends to cover them.
--
-- Scoped to the session: a harness id is only ever promised unique within
-- its own session, and a collision across two must never cost an event.
create unique index events_harness_key_uniq
  on events (owner_id, project, harness, session_id, event_key)
  where event_key is not null;
