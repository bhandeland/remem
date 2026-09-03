---
name: remem-fail-soft-hides-budget-failures
description: A RulesExceedBudget failure silently kills context injection on every remem harness; only REMEM_HOOK_DEBUG=1 reveals it.
metadata:
  type: project
---

When the knowledge base outgrows `REMEM_MAX_CHARS`, `kb` raises
`RulesExceedBudget`, and because every remem hook is fail-soft the hook exits 0 and
prints nothing. Context injection then dies **on every harness at once** - Claude
Code included - with no signal anywhere.

Found 2026-08-30: the block needed 12044 chars against a 6000 budget and had been
silently failing. Diagnose with `REMEM_HOOK_DEBUG=1 remem hook session-start`, which
writes the reason to stderr. Raised to 16000 via `remem config set REMEM_MAX_CHARS`.

**Why:** the fail-soft contract is right - a knowledge tool must never be why a
session will not start - but "the block was computed and then thrown away" is a
different class of failure from "the database was down", and only the second is what
silence was designed for.

**How to apply:** if a context block is missing anywhere, check the budget first, with
`REMEM_HOOK_DEBUG=1`.

Watch the right number. `kb.render` renders rules first and never truncates them - only
**rules + header** exceeding `max_chars` raises. Notes fill the remainder, are dropped
whole, and the block ends with `- N more entries not shown`. So the total block always
measures just under the budget whenever notes are waiting; that is the design working,
not a warning. As of 2026-08-30: rules + header 11,223 against 16,000, roughly 30%
headroom. Prune by moving off-topic entries to another project with
`remem update <id> --project <name>`, which is non-destructive and keeps them
searchable.
See [[cursor-runs-claude-code-hooks]].
