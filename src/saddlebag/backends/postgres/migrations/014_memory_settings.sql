-- Which collection is exported to Claude Code's memory directory, per
-- project. Shaped after record_settings - a per-project opt-in checked in
-- the service - but holding a value rather than a boolean, because the
-- question here is "which collection" and not "on or off". No row means
-- not designated, which means the sync writes nothing at all.
--
-- No foreign key to collections: a collection deleted out from under a
-- designation should leave the designation visibly dangling for the sync to
-- report, not cascade into silently un-designating a project.
create table memory_settings (
  owner_id uuid not null references principals(id),
  project text not null,
  collection_slug text not null,
  updated_at timestamptz not null default clock_timestamp(),
  primary key (owner_id, project)
);
