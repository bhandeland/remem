-- Semantic recall: one vector per (entry, embedding model).
--
-- A separate table, not a column on entries. Vectors are DERIVED data, and
-- this codebase's rule is that derived data lives apart from its source, is
-- recomputable, and never overwrites it. Changing embedding model is then
-- inserting rows and deleting old ones - never a migration, never a rewrite
-- of the source row - and two models can coexist while a re-embed runs.
-- Losing this table costs a re-run and no data.

create extension if not exists vector;

create table entry_vectors (
  entry_id   uuid not null references entries(id) on delete cascade,
  model      text not null,
  dim        int  not null,
  vector     vector not null,
  created_at timestamptz not null default clock_timestamp(),
  primary key (entry_id, model)
);

-- `dim` is stored even though it is derivable from the vector, because it is
-- what makes a mismatch loud. A query embedded by a different model than the
-- rows it is compared against produces a pgvector error on dimension, which
-- is right, but the recorded dim lets the service say WHICH model disagreed
-- rather than surfacing the raw operator error.

-- NO APPROXIMATE-NEAREST-NEIGHBOUR INDEX, DELIBERATELY.
--
-- pgvector cannot index a `vector` column of unspecified dimension, and
-- declaring one here would commit the schema to a single embedding model -
-- the exact coupling this table exists to avoid. The alternatives are a
-- partial index per model (with a cast to a fixed dimension) or a table per
-- dimension. Both were considered and both are premature: at this store's
-- size, exact nearest-neighbour by sequential scan takes milliseconds, and
-- building an HNSW index would cost more than the scans it replaces.
--
-- REVISIT WHEN: entry_vectors passes roughly 50k rows, or the document
-- corpus is imported - whichever comes first. At that point the model in use
-- is a known fact rather than a guess, which is precisely what makes the
-- choice between the two options decidable. Until then, exact search is not
-- a compromise; it is the more accurate of the two, and free.
