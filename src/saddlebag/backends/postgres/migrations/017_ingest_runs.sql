-- One row per ingest invocation - the spawned `remem reingest run` and the
-- typed `remem ingest` alike. This is the after-the-fact record a fail-soft,
-- detached refresh otherwise never leaves: before this table, `refresh`
-- computed counts, per-path failures and the embed error and handed them to
-- a debug channel whose stderr is /dev/null.
--
-- A started row with `finished_at` null is a statement, not a gap: the
-- process died between starting and finishing. That is what makes "crashed"
-- distinguishable from "never ran". It only works because the spawned run
-- commits the started row before doing any work (autocommit, like
-- `remem events process`).
--
-- Rows are kept indefinitely, as events are. Nothing prunes them.
create table ingest_runs (
  id uuid primary key,
  owner_id uuid not null references principals(id),
  project text not null,
  -- 'auto' for the spawned refresh, 'manual' for `remem ingest`. Text with
  -- a check rather than an enum: two values, and the check reads the same.
  trigger text not null check (trigger in ('auto', 'manual')),
  -- Which origin a manual run wrote. The refresh covers both halves in one
  -- row and records false.
  archive boolean not null default false,
  started_at timestamptz not null default clock_timestamp(),
  finished_at timestamptz,
  created int not null default 0,
  changed int not null default 0,
  unchanged int not null default 0,
  swept int not null default 0,
  embedded int not null default 0,
  -- [{"path": ..., "reason": ...}]. A designated path that no longer exists
  -- lands here on every run, which is how a renamed directory stops being
  -- a silent per-run failure.
  failures jsonb not null default '[]'::jsonb,
  -- [{"path": ..., "existing": ..., "live": n}] - files that came in
  -- entirely new while an anchor with the same filename already had live
  -- chunks under another src: path. See services/ingest.find_twin.
  twins jsonb not null default '[]'::jsonb,
  embed_error text
);

-- Every read is "the latest row for this project".
create index ingest_runs_latest_idx
  on ingest_runs (owner_id, project, started_at desc);
