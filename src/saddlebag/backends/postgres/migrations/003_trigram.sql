-- Trigram indexes for typo-tolerant search.
--
-- Full-text search matches lexemes, so a misspelled query matches nothing at
-- all. Trigram similarity closes that gap, but only as a FALLBACK: it runs
-- solely when FTS returns no rows, so fuzzy hits can never dilute exact ones.
--
-- Two different operators, because they behave very differently on the two
-- columns (measured, not assumed):
--   similarity(title, q)      -> 0.793 for a typo'd title
--   word_similarity(q, body)  -> 0.545 for a typo'd word inside a body
-- Plain similarity() on body is NOT usable: on a 3-row sample it scored the
-- WRONG document higher than the right one (0.074 vs 0.072), because a long
-- body dilutes whole-string similarity into noise. word_similarity compares
-- the query against the best-matching word sequence instead, which is what
-- makes body matching work at all.
--
-- Index cost measured at 20k entries: title ~1.7MB, body ~2.4MB.
-- Both are partial on the same invariant every other read uses.

create extension if not exists pg_trgm;

create index entries_title_trgm_idx on entries
  using gin (title gin_trgm_ops)
  where superseded_by is null;

create index entries_body_trgm_idx on entries
  using gin (body gin_trgm_ops)
  where superseded_by is null;
