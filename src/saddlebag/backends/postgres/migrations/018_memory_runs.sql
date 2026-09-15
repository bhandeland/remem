-- One row per `remem memory sync`, per project - the record a sync
-- otherwise leaves only in a terminal scrollback. Built before the
-- hook-spawned sync rather than after, because that caller is the one with
-- no terminal, and because a conflict sidecar outlives the run that wrote
-- it: nothing ever deletes a `.remem-conflict.md`, so an unresolved
-- conflict is a permanent condition visible today only from that project's
-- own directory.
--
-- A started row with `finished_at` null is a statement, not a gap: the
-- process died between starting and finishing. That only works because
-- `remem memory sync` commits the started row before reading any file,
-- which is why it moved to an autocommit session.
--
-- Rows are kept indefinitely, as events and ingest_runs rows are. Nothing
-- prunes them.
create table memory_runs (
  id uuid primary key,
  owner_id uuid not null references principals(id),
  project text not null,
  -- 'manual' for `remem memory sync`, 'auto' for the hook-spawned sync that
  -- does not exist yet. Recorded now because that caller is the reason this
  -- table exists, and a column added later costs a migration for a case
  -- already known to be coming. Text with a check rather than an enum: two
  -- values, and the check reads the same.
  trigger text not null check (trigger in ('manual', 'auto')),
  started_at timestamptz not null default clock_timestamp(),
  finished_at timestamptz,
  adopted int not null default 0,
  healed int not null default 0,
  edited int not null default 0,
  regenerated int not null default 0,
  deleted int not null default 0,
  unchanged int not null default 0,
  -- [["old","new"], ...]. Named rather than only counted: a rename re-tags
  -- an entry, which is a write the user did not ask for by name.
  renamed jsonb not null default '[]'::jsonb,
  -- Names changed on both sides.
  conflicts jsonb not null default '[]'::jsonb,
  -- The subset of `conflicts` that actually wrote a `.remem-conflict.md`.
  -- A dry run writes none, and neither does a file edited for an entry that
  -- left the collection, so the two lists are not interchangeable.
  sidecars jsonb not null default '[]'::jsonb,
  -- [{"name": ..., "reason": ...}]. A Python exception is recorded at the
  -- name '*'.
  failures jsonb not null default '[]'::jsonb
);

-- Every read is "the latest row for this project".
create index memory_runs_latest_idx
  on memory_runs (owner_id, project, started_at desc);
