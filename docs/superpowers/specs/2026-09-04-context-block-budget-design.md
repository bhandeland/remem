# Keeping the context block bounded as rules accumulate

Design, 2026-09-04.

## The problem

Context injection is dead on this machine, for the second time in three days:

```
RulesExceedBudget: rules and header need 26108 chars, budget is 24000
```

Every harness, exit 0, no output - the failure mode `remem-fail-soft-hides-budget-failures`
already records. The budget was raised from 16,000 to 24,000 on 2026-09-02 and
was exhausted by 2026-09-04:

| | rules | chars |
|---|---|---|
| 2026-09-02 | 14 | 18,249 |
| 2026-09-04 | 19 | 25,008 |

That is 6,759 chars in 48 hours, roughly 3,400 a day. Raising the number again
buys two days. It has been raised twice and is not a tuning problem.

### Why the rules are large

Rules in this project are essays, and the essay is the point. The largest is
2,077 chars and opens:

> The most expensive defect in the opencode-adapter branch, and the one the
> final whole-branch review was hired to find. Neither task that caused it was
> wrong.

The actual instruction is the title, plus a short "So:" list at the end. In
between is the case: which branch, which ruling, what the tests actually did.
The other three largest rules open the same way - "Four times in one seven-task
branch...", "Found 2026-09-01 shipping `remem doctor`...", "Measured over a
ten-task subagent-driven run...".

So every session pays for case history that nothing reads unless someone asks
*why*. The block should carry the rule; the entry should keep the case.

## What gets built

`kb.render` renders rules as title plus summary rather than title plus body.
`services/write.py` requires a summary on a rule. `write.update` learns about
summaries so the existing 19 can be backfilled in place. `REMEM_MAX_CHARS`
comes back down. No migration - `Entry.summary` already exists.

## Decisions

### The short form is the title and the summary, never derived from the body

Deriving it would need no new data, so it was measured first. It does not work:
the first paragraph of a rule is the incident, not the instruction. All four of
the largest rules open with case history, so a body-derived block would hand an
agent the anecdote and withhold the rule - strictly worse than the title alone.

Titles, by contrast, are already written as directives: "Run remem ingest from
the repository root, never a subdirectory", "A diagnostic must name where it
looked, or it will eventually lie confidently". All 19 titles together are
1,419 chars, averaging 74.

| rendering | chars |
|---|---|
| 19 full bodies (today) | 25,008 |
| titles and id lines only | ~2,300 |
| titles, summaries and id lines (estimated) | ~4,200 |

A rule with no summary renders **title only**. That is the graceful floor: the
17 rules that have no summary today keep working, and the block improves as
summaries are written rather than requiring an editorial pass before anything
can ship.

The `_id:` line is rendered in both cases. It is how an agent fetches the full
rule, and dropping it would make the block a dead end rather than an index.

The rendered shape, exactly - the same `### title`, blank line, content, blank
line, `_id:` structure `_render_entry` already produces, with the summary
standing where the body used to:

```
### A test marker describes what a test does, not whether it still passes

Mark by behaviour: if a test's call graph can open a socket, touch a file
outside tmp, or spawn a process, its marker says so - green or not.

_id: 01a04d1e-...
```

and with no summary, the content line is simply absent:

```
### Run remem ingest from the repository root, never a subdirectory

_id: 01a06f1a-...
```

Tags render as they do today, on the `_id:` line, for both kinds.

### `Entry.summary` is the right field, and its docstring is stale

The field's comment claims it belongs to Claude Code memory frontmatter:

```python
#: The one-line description a Claude Code memory file carries in its
#: frontmatter. Nullable because every other origin has no such thing.
```

That stopped being true at `e6eb730`, which put `--summary` on `remember`,
`rule` and `supersede` precisely because an entry authored in remem "exported
with an empty frontmatter description". It is a general one-line-description
field with two readers, and the comment should say so. Adding a second,
parallel field would be the actual overload.

### Enforcement lives in `services/write.py`, not in `cli.py`

`remem rule` refusing without `--summary` cannot be a CLI check. MCP's
`remember_tool(title, body, kind="note", ...)` accepts a `kind` and will write
`kind="rule"` happily, and it has no summary parameter at all. A CLI-side check
leaves a hole the MCP server walks straight through - which is the failure the
layering rule in CLAUDE.md exists to prevent.

So `write.remember` raises when `kind is Kind.RULE` and no summary is given,
and both frontends inherit it. `remember_tool` gains a `summary` parameter, or
writing a rule through MCP becomes impossible.

Rules are the only kind that lands in every session's context, so they are the
only kind forced to state themselves in a line. `NOTE` and `DOC` are unaffected.

### `write.update` gains `summary`, because backfill must not supersede

`write.update` takes title, body, tags and project. It does not take a summary,
so today the only way to put one on an existing rule is `supersede` - which
retires the entry and mints a replacement.

Backfilling 17 rules that way would create 17 entries, retire 17 good ones, and
churn every corresponding memory file, because `summary` feeds the frontmatter
`description`. All of that for an editorial addition. `update`'s own docstring
says what it is for: "edit an entry in place (for typos)". A missing summary is
exactly that.

No `--clear-summary`. A wrong summary is fixed by writing a better one, and the
`_Clear` sentinel is already there if that proves wrong.

### The budget comes back down to 8,000

Leaving `REMEM_MAX_CHARS` at 32,000 after the block drops to roughly 4,200
would leave 27KB of headroom for the block to refill into. It cannot today -
the remem collection resolves to 19 rules and zero non-rule entries, so the
`## Knowledge` half renders nothing - but a collection query that later admits
notes would silently restore the block to its old size, and the failure would
read as normal.

The budget should describe what the block ought to cost, not the high-water
mark of what it once did.

### The never-truncate invariant is kept, not weakened

`RulesExceedBudget` stays, and so does the reasoning on it: an agent given a
partial rule proceeds believing it has the conventions, which is worse than
having none. What changes is that rules become cheap enough that the exception
is a genuine backstop rather than a twice-weekly event.

The alternative - dropping rules with a notice, the way non-rule entries
already degrade - was rejected for that same reason. It would make the common
case "some rules are missing and the block says so in one line at the bottom",
which is precisely the partial-conventions failure.

## Architecture

Five files, each with one responsibility, no new module:

| file | change |
|---|---|
| `services/kb.py` | `_render_entry` renders a rule as title + summary + id; body for every other kind |
| `services/write.py` | `remember` raises on a rule without a summary; `update` accepts `summary` |
| `mcp_server.py` | `remember_tool` gains `summary`; a rule without one returns an error dict |
| `cli.py` | `remem update --summary` |
| `domain.py` | correct the `Entry.summary` comment |

`_render_entry` is currently kind-blind. It becomes kind-aware, which is the
one piece of new policy, and it belongs in `kb.py` alongside the rules-first
ordering that is already there.

## Error handling

A rule written without a summary is a **loud** failure everywhere, because a
person or an agent is asking for it at that moment:

- `write.remember` raises `RuleNeedsSummary`, a new exception beside
  `EntryNotFound` and `CannotLinkToSelf`.
- `remem rule` exits non-zero, naming the flag.
- `remember_tool` returns `{"error": ...}` rather than raising, matching how
  `_invalid_kind_message` already reports a bad kind. An MCP tool that raises
  gives the model a stack trace where a sentence would do.

Rendering, by contrast, never fails on a missing summary - it falls back to the
title. The write path is where the requirement is enforced, so the read path
does not need to restate it, and the 17 existing rules must keep rendering.

## Testing

`kb.render` is pure, so its tests carry no `db` marker and run on CI - the same
split `markdown.py` uses:

- a rule with a summary renders title and summary, and **not** its body
- a rule without a summary renders title only
- the `_id:` line survives both cases
- a note still renders its body, unchanged
- `test_rules_are_never_truncated_even_over_budget` still holds, now about
  short forms
- `test_rules_alone_exceeding_the_budget_raises` needs rebuilding: it must
  still construct the over-budget case now that rules are cheap

`db`-marked, for enforcement:

- `write.remember` with `kind=RULE` and no summary raises; `NOTE` and `DOC` do not
- `write.update` sets a summary without creating a second entry
- `remember_tool(kind="rule")` with no summary returns an error dict
- `remem rule` without `--summary` exits non-zero

One check is deliberately **not** a test: that the real 19-rule collection
renders under 8,000 chars. It is a hand-run verification after implementing,
because as a test it would fail the moment a twentieth rule is written.

## Out of scope

- Backfilling summaries onto the 17 rules that lack one. Title-only is a
  working floor; summaries get written as each rule next comes up.
- Ranking, scoring or selecting which rules appear. Every rule still appears.
- Per-project rule scoping. The collection query already scopes them and all 19
  belong to this project.
- Making the fail-soft budget failure visible. That is real and recorded
  separately, and it is a different piece of work: this design makes the
  failure rare, not loud.
