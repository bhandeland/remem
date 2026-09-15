-- Ingested document chunks are ordinary entries with their own origins, so
-- they can be held out of context blocks and (for plans) out of default
-- search without a second table.
--
-- Two values rather than one: origin is the only default-hiding mechanism
-- remem has, because DEFAULT_ORIGINS is an allowlist and an exclude filter
-- was deliberately declined (see services/search.py). 'ingested' is in that
-- list, 'archived' is not.
--
-- This migration adds values and writes NO row that carries them. That is
-- required, not stylistic: as 005_handoff.sql records, a value added by
-- `add value` cannot be USED in the transaction that added it unless the
-- type was created there too, and migrate() runs every pending migration in
-- one transaction.
--
-- `if not exists` because a database migrated by a build that already
-- carried these values must not fail here.
alter type entry_origin add value if not exists 'ingested';
alter type entry_origin add value if not exists 'archived';
