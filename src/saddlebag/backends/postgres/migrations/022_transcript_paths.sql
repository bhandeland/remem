-- Which transcript directories a project claims.
--
-- Recording is opt-in per project and that gate governs recording going
-- FORWARD. A claim here is the separate, deliberate opt-in for BACKFILL,
-- because claiming a directory imports all of it - including sessions that
-- predate the recording pipeline entirely. Nothing auto-claims; `discover`
-- proposes and writes nothing.
--
-- Paths are stored ABSOLUTE, as given. This is deliberately unlike
-- ingest_designations, which stores repo-relative paths and resolves them
-- against the git root: these directories live outside any repository and
-- there is no root to resolve against.
create table transcript_paths (
  owner_id uuid not null references principals(id),
  project text not null,
  path text not null,
  added_at timestamptz not null default clock_timestamp(),
  primary key (owner_id, project, path)
);

-- A directory belongs to at most one project. Two projects claiming the same
-- directory would file the same session twice under different projects, and
-- the unique constraint on transcripts would then reject the second import
-- with nothing explaining why.
create unique index transcript_paths_one_owner_idx on transcript_paths (owner_id, path);
