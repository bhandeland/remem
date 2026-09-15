-- Partial indexes matching remem's always-on read filters.
--
-- Every entry read constrains owner_id and excludes superseded rows. Before
-- these indexes both predicates were rechecked against the heap after the
-- index scan; encoding them in the index means it holds only rows a query
-- could actually return.
--
-- Measured against a 60k-entry corpus (3 owners, 15% superseded):
--   ranked text search   14.8ms -> 7.4ms   (shared buffers 4147 -> 2944)
--   no-text listing       8.9ms -> 0.03ms
-- Combined size ~5.8MB against a 59MB table.

-- Lets a single GIN index mix the scalar owner_id with the tsvector.
-- Needs superuser the first time; a no-op once present.
create extension if not exists btree_gin;

-- Ranked text search: owner and tsvector in one index, live rows only.
create index entries_owner_search_live_idx on entries
  using gin (owner_id, search)
  where superseded_by is null;

-- No-text listing and knowledge-base resolution. The index supplies the
-- ordering, so LIMIT stops after N rows instead of sorting the owner's whole
-- corpus. This is the path the SessionStart hook takes on every session.
create index entries_owner_recent_idx on entries (owner_id, created_at desc)
  where superseded_by is null;
