-- The vocabulary the events design settled: 'memory' becomes 'note',
-- 'capture' becomes 'extracted', and capture_settings becomes
-- record_settings. Names only - no table gains or loses a column here.
--
-- ALTER TYPE ... RENAME VALUE is transactional, unlike ADD VALUE, so this
-- runs safely inside migrate()'s caller-owned transaction. It rewrites no
-- rows: the label changes, the ordinal does not, and every existing row
-- reads back under the new name for free.

alter type entry_kind rename value 'memory' to 'note';
alter type entry_origin rename value 'capture' to 'extracted';

-- THE GAP THE RENAME DOES NOT CLOSE.
--
-- collections.query is jsonb. A smart collection filtering on kinds stores
-- the literal string "memory" inside that JSON, and renaming the enum label
-- does not reach inside it. Left alone, every such collection would match
-- nothing after this migration - and an empty query matching nothing,
-- forever, is precisely this codebase's documented sharp edge, arrived at
-- once already by accident. It is also the one line a reader would never
-- think to look for, which is why it is spelled out rather than folded in.
update collections
   set query = jsonb_set(
         query,
         '{kinds}',
         (select coalesce(
                   jsonb_agg(
                     case when k = '"memory"'::jsonb
                          then '"note"'::jsonb
                          else k end
                   ),
                   '[]'::jsonb)
            from jsonb_array_elements(query -> 'kinds') as k)
       )
 where query ? 'kinds'
   and query -> 'kinds' @> '["memory"]'::jsonb;

-- A live table, not a dead one: record_settings holds the per-project
-- opt-in, which is the entire privacy story of the events pipeline and is
-- read on the path of every recorded event. The shape is unchanged, so this
-- is a bare rename with nothing to get wrong in SQL. The risk is a PARTIAL
-- rename - three statements in backends/postgres/store.py name this table
-- in string SQL, where no type checker will catch a miss, and a missed one
-- fails the opt-in lookup rather than erroring. A fail-soft hook then
-- records nothing, silently. All three move in the same commit as this file.
alter table capture_settings rename to record_settings;
