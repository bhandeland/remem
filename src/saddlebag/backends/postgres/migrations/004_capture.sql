-- Automatic session capture: opt-in settings and the job spool.
--
-- The spool IS the queue. It is deliberately a table rather than a broker:
-- it doubles as the audit trail that `remem capture status` reads, and it
-- needs no process running for a capture to be recorded.

create type capture_status as enum ('pending','running','done','failed');

create table capture_settings (
  owner_id uuid not null references principals(id),
  project  text not null,
  enabled  boolean not null default true,
  created_at timestamptz not null default clock_timestamp(),
  primary key (owner_id, project)
);

-- transcript_path, not the transcript: transcripts reach megabytes and already
-- live on disk under ~/.claude/projects/. A missing file at drain time is a
-- readable failure, not a silent success.
create table capture_jobs (
  id uuid primary key,
  owner_id uuid not null references principals(id),
  project text not null,
  session_id text,
  transcript_path text not null,
  status capture_status not null default 'pending',
  attempts int not null default 0,
  error text,
  entries_written int not null default 0,
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp()
);

create index capture_jobs_pending_idx on capture_jobs (owner_id, created_at)
  where status = 'pending';
