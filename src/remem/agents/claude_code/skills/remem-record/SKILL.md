---
name: remem-record
description: >
  Use when the user asks why remem is not recording anything, why entries look
  machine-written, or how automatic recording and extraction work - and when
  turning recording on or off for a project, or recovering an extraction job
  that failed.
---

# remem record

remem can record raw events as you work and later extract durable entries
from them automatically, so durable knowledge survives without anyone
remembering to write it down. It is separate from the `remem` skill: that
one is you deciding to write something, this one is the machine doing it
after you have gone.

## It is off until someone turns it on

Recording is **opt-in per project**. A project nobody enabled records
nothing, forever, and says nothing about it.

That is almost always the answer to "why is remem not recording here":

```bash
remem record status          # is this project enabled, and what's stuck?
remem record enable          # turn it on for the current directory
remem record disable         # turn it off again
```

The gate is deliberate, not an oversight. Recording writes every tool call's
raw payload - full command output, file contents, whatever a user pasted -
into the database; that should never happen to a project the user did not
choose. Do not enable it for the user without asking - it is their call, not
yours.

## How it runs

Events are recorded one at a time, in full, and kept **indefinitely** -
nothing prunes them on a schedule. A session becomes extractable once it has
gone quiet for `REMEM_IDLE_MINUTES` (default 20) - not when it ends, because
not every harness has a session-end hook to trigger on. `remem events
process` does the extraction, run from cron or a later SessionStart:

```bash
remem events process          # extract from whatever session has gone quiet
```

So entries from today's session usually appear once it has been idle for a
while, not the moment it ends. That lag is expected; it is not a failure.

The extraction model is pinned (`REMEM_EXTRACT_MODEL`, default `sonnet`)
rather than inherited from the session, so cost and judgment do not drift
when the user switches models for unrelated reasons.

## Extracted entries do not appear in context blocks

This surprises people, so state it plainly when it comes up: entries with
`origin=extracted` are **excluded from knowledge base context blocks**.
Machine text must not crowd out hand-written rules.

They do appear in `remem search` and `remem recall`. So an extracted entry
that you can find but that never shows up at session start is working
correctly.

To promote one into the context block, pin it:

```bash
remem kb pin <slug> <entry-id>
```

## When a job fails

```bash
remem record status
```

Failures record both the reason and the model's raw output, so a job that
produced unusable JSON can be diagnosed rather than guessed at.

Jobs stop retrying after the attempt cap. Past that point the only way back
is by id:

```bash
remem events process --job <id>
```

which retries that job however many times it has already failed.

## What to tell the user

- Nothing recorded, project not enabled - say so and offer `remem record
  enable`; do not run it unasked.
- Extracted entry missing from a context block - working as designed, offer
  `remem kb pin`.
- A failed job - read the recorded reason before retrying. Retrying a job
  that failed on bad output just spends the same tokens again.

## If they still type `remem capture ...`

`remem capture enable|disable|status|drain` still run - they warn once to
stderr naming the replacement, then delegate. Point the user at the new
names above rather than at the deprecated ones.
