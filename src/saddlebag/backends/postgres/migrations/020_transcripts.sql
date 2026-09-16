-- The raw session transcript, byte for byte, as the source everything else
-- is derived from. The events pipeline records the tool layer and nothing
-- else - claude-code has thousands of tool_call rows and zero message rows -
-- so the prompts, the assistant text and the reasoning between the calls
-- have never been in the database at all. The full trace was always on disk
-- as JSONL, and every session_end payload already carries its path.
--
-- This finishes the decision the events design started rather than reversing
-- it: "derived data lives apart from its source, is recomputable, and never
-- overwrites it". Events fixed recomputability for the tool layer; this fixes
-- it for the rest.
--
-- Kept indefinitely, like events. Nothing prunes a row on a schedule.
create table transcripts (
  id uuid primary key,
  owner_id uuid not null references principals(id),
  project text not null,
  -- Not a promise. Claude Code is the only harness that writes a transcript
  -- today; the column exists so a second one needs no migration, and the
  -- table is simply empty for cursor and opencode. Nothing downstream may
  -- require a transcript to exist.
  harness text not null,
  session_id text not null,
  path text not null,
  -- bytea, not text. A transcript is a file another tool owns, and if one
  -- line is ever invalid UTF-8 then `text` refuses the insert and the whole
  -- session is lost rather than the line. The entire point of this row is
  -- that it survives a parser that does not.
  content bytea not null,
  bytes bigint not null,
  -- sha256 of `content`. The append check reads the first `bytes` bytes of
  -- the file on disk and compares: a match proves the file was appended to
  -- rather than rewritten, which is what lets an import add lines instead of
  -- rebuilding them.
  sha256 text not null,
  first_seen timestamptz not null default clock_timestamp(),
  last_read timestamptz not null default clock_timestamp(),
  -- Session id, not path, is identity. Two claimed directories hold
  -- different sessions, so path cannot be the key - but the same session
  -- must never land twice if two projects claim overlapping directories.
  -- This is also already how `events` identifies a session.
  unique (owner_id, harness, session_id)
);

create index transcripts_project_idx on transcripts (owner_id, project);
