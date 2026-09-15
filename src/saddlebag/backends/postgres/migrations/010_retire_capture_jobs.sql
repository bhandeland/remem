-- The old spool, renamed and left alone.
--
-- Not dropped: a pending capture job names a transcript a user may still
-- want extracted by hand, and deleting it silently is the one thing this
-- migration must not do. Not translated either - see 009 for why. So it
-- sits here, unread, with no code path referring to it, until the user
-- confirms they want nothing from it and a later migration drops it.
-- `remem record status` mentions it once if any row is still pending, so
-- the dead spool is visible rather than mysterious.
--
-- capture_status is deliberately left in place: it is this table's column
-- type, and mutating an enum a live table still uses buys nothing here.

alter table capture_jobs rename to capture_jobs_legacy;
alter index capture_jobs_pending_idx rename to capture_jobs_legacy_pending_idx;
