-- Which paths a project re-ingests, so a spawned refresh can answer
-- "which paths, and which are archive" with no human at the keyboard.
--
-- Shaped after memory_settings - a per-project opt-in holding a value
-- rather than a boolean - with `archive` in the primary key, because the
-- refresh incantation is two invocations with different flags and not one.
-- No row means not designated, which means the spawned refresh does
-- nothing at all: that silence is the opt-in.
--
-- Paths are stored REPO-RELATIVE and resolved against the git root at
-- refresh time. Unlike memory_settings this needs no recorded working
-- directory: ingest already resolves its project from the git common dir,
-- so a worktree and its main checkout share both the project and the
-- relative paths, and the root is derivable at the point of use.
create table ingest_settings (
  owner_id uuid not null references principals(id),
  project text not null,
  archive boolean not null,
  paths text[] not null,
  updated_at timestamptz not null default clock_timestamp(),
  primary key (owner_id, project, archive)
);
