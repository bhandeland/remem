---
name: remem-capture
description: >
  Use when the user asks why remem is not recording anything, why entries look
  machine-written, or how automatic capture works - and when turning capture on
  or off for a project, or recovering a capture job that failed.
---

# remem capture

Capture distils a finished session into entries automatically, so durable
knowledge survives without anyone remembering to write it down. It is separate
from the `remem` skill: that one is you deciding to write something, this one
is the machine doing it after you have gone.

## It is off until someone turns it on

Automatic capture is **opt-in per project**. A project nobody enabled records
nothing, forever, and says nothing about it.

That is almost always the answer to "why is remem not recording here":

```bash
remem capture status          # is this project enabled at all?
remem capture enable          # turn it on for the current directory
remem capture disable         # turn it off again
```

The gate is deliberate, not an oversight. Capture sends a session transcript to
a model; that should never happen to a project the user did not choose. Do not
enable it for the user without asking - it is their call, not yours.

## How it runs

The SessionEnd hook does one thing: queue a job. Everything that can be slow or
fail is deferred to a later SessionStart, or to an explicit drain:

```bash
remem capture drain           # distil whatever is queued
```

So entries from today's session usually appear at the *start* of the next one,
not the end of this one. That lag is expected; it is not a failure.

The distillation model is pinned (`REMEM_CAPTURE_MODEL`, default `sonnet`)
rather than inherited from the session, so cost and judgment do not drift when
the user switches models for unrelated reasons.

## Captured entries do not appear in context blocks

This surprises people, so state it plainly when it comes up: entries with
`origin=capture` are **excluded from knowledge base context blocks**. Machine
text must not crowd out hand-written rules.

They do appear in `remem search` and `remem recall`. So a captured entry that
you can find but that never shows up at session start is working correctly.

To promote one into the context block, pin it:

```bash
remem kb pin <slug> <entry-id>
```

## When a job fails

```bash
remem capture status
```

Failures record both the reason and the model's raw output, so a job that
produced unusable JSON can be diagnosed rather than guessed at.

Jobs stop retrying after the attempt cap. Past that point the only way back is
by id:

```bash
remem capture drain --job <id>
```

which retries that job however many times it has already failed.

## What to tell the user

- Nothing captured, project not enabled - say so and offer `remem capture
  enable`; do not run it unasked.
- Captured entry missing from a context block - working as designed, offer
  `remem kb pin`.
- A failed job - read the recorded reason before retrying. Retrying a job that
  failed on bad output just spends the same tokens again.
