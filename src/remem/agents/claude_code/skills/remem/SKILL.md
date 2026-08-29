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

Session handoffs are excluded from these results by default - add
`--handoff` to search them too, or use the `remem-prime` skill to resume from
one directly.

4. **Answer** in plain language. Do not dump raw search output at the user.

## When to write an entry

This is the discipline that keeps the store useful. Write when:

- A decision was made and the reasoning is not obvious from the code.
- You hit a non-obvious gotcha that cost real time.
- The user states a preference or convention that should hold in future.
- You learned how a system behaves in a way the repo does not document.

### Write it in the moment, without being asked

Four things should make you reach for `remember` immediately, in the same turn
they happen. Do not wait to be told, and do not save them for the end of the
session - a convention recorded three turns later is usually recorded wrong,
and one recorded at the end is usually not recorded at all.

1. **The user corrects you.** "No, we use X here", "don't do Y in this repo".
   A correction is a convention you did not know. Write it as `kind: rule`.
2. **The user states a preference.** Formatting, naming, tooling, tone -
   anything phrased as how things are done here rather than what to do now.
3. **Something cost real time.** A wrong assumption, a confusing error, a
   non-obvious fix. Write what you would have wanted to know an hour ago.
4. **The user says "remember", "note that", or "for future reference".** That
   is an explicit instruction - act on it rather than acknowledging it.

Tell the user in one short line when you have written something, so a wrong
entry can be corrected while it is still cheap to fix. Do not ask permission
first for a rule the user just stated; they already told you.

Omit `project` and the current project is used. Only pass it to file something
under a different one.

Do **not** write:

- Anything already recorded in the code, the README, or git history.
- Details that only matter for the current task.
- Restatements of something already stored - use `supersede` instead when the
  stored version is now wrong.

```bash
remem remember "Postgres pool sizing" --body "..." --kind note --project myapp
remem remember "Never use em dashes" --body "..." --kind rule --project myapp
```

Rules are always injected into future sessions, so keep them few and sharp.

## Correcting stale knowledge

When you find a stored entry that is no longer true, replace it rather than
adding a contradiction:

```bash
remem supersede <id> --title "New title" --body "What is true now"
```

## Settings

`remem config list` shows every remem and Claude Code setting, its value, and
whether it came from the environment, a file, or a default.

```bash
remem config set BASH_DEFAULT_TIMEOUT_MS 10m
remem config unset DISABLE_TELEMETRY
```

If the user asks why a setting they changed had no effect, run `remem config
get <key>` - a `source` of `environment` on a remem key means an export is
shadowing the file.

## Knowledge bases

A knowledge base is a curated set of entries rendered as one context block.

```bash
remem kb list
remem kb show <slug>
remem kb pin <slug> <entry-id>
```
