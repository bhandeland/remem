---
name: remem
description: >
  Use when the user asks what you remember about a project, refers to a past
  decision, asks "have we solved this before", or when you learn something
  durable that should outlive this session. Searches and writes the user's
  remem knowledge store.
---

# remem

A persistent knowledge store shared across sessions. It holds three kinds of
entry: **memories** (things learned), **docs** (reference material), and
**rules** (conventions that must be followed).

## Searching

Work cheap passes first, expensive detail only for confirmed hits.

1. **Broad pass.** One to three keyword searches - project names, file names,
   error strings, feature names. Skim titles and snippets only.

   ```bash
   remem search "connection pool" --limit 20
   ```

2. **Narrow pass.** Re-search with the most specific terms the broad pass
   surfaced (exact phrases, identifiers), with a smaller limit.

3. **Read.** Fetch only the entries worth reading in full.

   ```bash
   remem get <id>
   ```

4. **Answer** in plain language. Do not dump raw search output at the user.

## When to write a memory

This is the discipline that keeps the store useful. Write when:

- A decision was made and the reasoning is not obvious from the code.
- You hit a non-obvious gotcha that cost real time.
- The user states a preference or convention that should hold in future.
- You learned how a system behaves in a way the repo does not document.

Do **not** write:

- Anything already recorded in the code, the README, or git history.
- Details that only matter for the current task.
- Restatements of something already stored - use `supersede` instead when the
  stored version is now wrong.

```bash
remem remember "Postgres pool sizing" --body "..." --kind memory --project myapp
remem remember "Never use em dashes" --body "..." --kind rule --project myapp
```

Rules are always injected into future sessions, so keep them few and sharp.

## Correcting stale knowledge

When you find a stored entry that is no longer true, replace it rather than
adding a contradiction:

```bash
remem supersede <id> --title "New title" --body "What is true now"
```

## Knowledge bases

A knowledge base is a curated set of entries rendered as one context block.

```bash
remem kb list
remem kb show <slug>
remem kb pin <slug> <entry-id>
```
