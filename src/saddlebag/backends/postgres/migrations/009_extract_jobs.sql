-- The extraction spool. A NEW table, not a re-keyed capture_jobs: the old
-- key is a transcript path and the new one is a session, and the two do not
-- convert. A pending capture job names a transcript; the extractor's input
-- is events, which do not exist for that session and never will. Any
-- translation would be inventing rows. capture_jobs is retired untouched in
-- 010 instead.

-- A new enum rather than reusing capture_status, which 010 leaves in place
-- for the legacy table. Mutating an enum a live table still uses, in the
-- same migration that renames that table, is more moving parts than the
-- saving is worth.
create type job_status as enum ('pending','running','done','failed');

create table extract_jobs (
  id uuid primary key,
  owner_id uuid not null references principals(id),
  project text not null,
  harness text not null,
  session_id text not null,
  -- The newest occurred_at among the events this job actually read.
  -- "This session has a done job" would be the obvious definition of
  -- extracted, and it is wrong for a resumed session: events recorded after
  -- that job would be born already-extracted, invisible to process and
  -- eligible for prune having produced nothing. The watermark makes the
  -- question answerable per event rather than per session.
  covers_through timestamptz,
  status job_status not null default 'pending',
  attempts int not null default 0,
  error text,
  entries_written int not null default 0,
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  unique (owner_id, project, harness, session_id)
);

create index extract_jobs_pending_idx on extract_jobs (owner_id, created_at)
  where status = 'pending';
