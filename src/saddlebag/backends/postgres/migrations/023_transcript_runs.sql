-- One row per import invocation, typed or spawned alike, written by the
-- service so both record identically - the spawned one being the caller with
-- no terminal, and the reason this table exists before it does.
--
-- A started row with `finished_at` null is a statement, not a gap: the
-- process died between starting and finishing, which is what makes "crashed"
-- distinguishable from "never ran". It only works because both commands open
-- with autocommit and commit the started row before reading any file.
create table transcript_runs (
  id uuid primary key,
  owner_id uuid not null references principals(id),
  project text not null,
  -- 'auto' for the spawned refresh, 'manual' for `bag transcripts import`.
  -- Text with a check rather than an enum: two values, and the check reads
  -- the same. A reader cannot otherwise tell an unattended run from their
  -- own, and believing the automatic half ran when only a manual one had is
  -- the false premise `status` names the trigger to prevent.
  trigger text not null check (trigger in ('auto', 'manual')),
  started_at timestamptz not null default clock_timestamp(),
  finished_at timestamptz,
  files_seen int not null default 0,
  files_new int not null default 0,
  files_appended int not null default 0,
  files_rebuilt int not null default 0,
  lines_written bigint not null default 0,
  bytes_written bigint not null default 0,
  -- [{"path": ..., "stored": n, "on_disk": n}] - files that shrank. The
  -- import refuses to follow these: the stored copy is more complete than
  -- what is on disk, and the purpose of the source row is that a rotating
  -- file does not destroy the session.
  anomalies jsonb not null default '[]'::jsonb,
  -- [{"path": ..., "reason": ...}]. A line that is not valid JSON lands here
  -- too, with its seq in the reason, so no line is ever silently dropped.
  failures jsonb not null default '[]'::jsonb
);

create index transcript_runs_latest_idx
  on transcript_runs (owner_id, project, started_at desc);
