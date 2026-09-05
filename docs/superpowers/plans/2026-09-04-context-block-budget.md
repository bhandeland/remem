# Context Block Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render rules in the context block as title plus summary instead of full bodies, so the block stops outgrowing its budget every two days.

**Architecture:** `kb._render_entry` becomes kind-aware: a rule renders its summary where its body used to go, and title-only when it has no summary. `services/write.py` requires a summary on new rules so the block keeps improving, and gains `update(summary=...)` so the 19 existing rules can be backfilled in place rather than superseded. Both frontends inherit the requirement because it lives in the service.

**Tech Stack:** Python 3.14, Typer, FastMCP, psycopg 3, Postgres 18, pytest.

**Spec:** `docs/superpowers/specs/2026-09-04-context-block-budget-design.md`

## Global Constraints

- Python 3.14. `from __future__ import annotations` at the top of every module.
- Strict layering, downward calls only: `cli.py`/`mcp_server.py` -> `services/` -> `store.py` -> `domain.py`. **No policy branch in a frontend** - the rule-needs-a-summary check lives in `services/write.py` precisely because `remember_tool` accepts `kind="rule"` and a CLI-side check would leave a hole.
- `services/kb.py` is pure - no store, no I/O - so its tests carry **no `db` marker** and must run on CI.
- Prose and comments use spaced hyphens ` - `, never em dashes.
- Comments explain *why*, at length, wherever a decision looks arbitrary.
- No migration. `Entry.summary` already exists on the model and in the schema.
- Rules keep the never-truncate invariant. `RulesExceedBudget` is not removed, softened, or turned into a notice.
- A green pytest run means nothing unless the skip count is zero. Check it.

---

### Task 1: Render rules as title and summary

**Files:**
- Modify: `src/remem/services/kb.py` (`_render_entry`, around line 148)
- Test: `tests/test_kb_render.py`

**Interfaces:**
- Consumes: `domain.Entry` (already has `summary: str | None`), `domain.Kind`.
- Produces: no new names. `_render_entry(entry: Entry) -> str` keeps its signature and changes behaviour for `Kind.RULE` only.

- [ ] **Step 1: Extend the test helper and write the failing tests**

`tests/test_kb_render.py` already has an `entry()` helper. It has no `summary`, so give it one, then add the new tests. Replace the existing helper:

```python
def entry(title, body, kind=Kind.NOTE, tags=None, summary=None):
    return Entry(id=new_id(), kind=kind, title=title, body=body,
                 owner_id=OWNER, tags=tags or [], summary=summary)
```

Add these tests to the same file:

```python
def test_a_rule_renders_its_summary_and_not_its_body():
    """The block carries the rule; the entry keeps the case for it.

    Rule bodies in this project are essays - the incident, the reasoning,
    the lesson - and shipping all of them is what kept blowing the budget.
    """
    e = entry("Mark tests by behaviour", "y" * 2000, kind=Kind.RULE,
              summary="If a test can open a socket, its marker says so.")

    out = render(collection(), [e], max_chars=5000)

    assert "Mark tests by behaviour" in out
    assert "If a test can open a socket, its marker says so." in out
    assert "y" * 2000 not in out


def test_a_rule_without_a_summary_renders_title_only():
    """The graceful floor: 17 rules predate the summary requirement and
    must keep rendering rather than vanishing or dragging their bodies in."""
    e = entry("Run ingest from the repository root", "z" * 900, kind=Kind.RULE)

    out = render(collection(), [e], max_chars=5000)

    assert "Run ingest from the repository root" in out
    assert "z" * 900 not in out


def test_a_rule_keeps_its_id_line_with_and_without_a_summary():
    """The id is how an agent fetches the full rule. Without it the block
    is a dead end rather than an index into the knowledge base."""
    with_summary = entry("A", "body", kind=Kind.RULE, summary="short form")
    without = entry("B", "body", kind=Kind.RULE)

    out = render(collection(), [with_summary, without], max_chars=5000)

    assert str(with_summary.id) in out
    assert str(without.id) in out


def test_a_note_still_renders_its_body():
    """Only rules change. Notes and docs are not injected into every
    session, so nothing about their cost changed."""
    e = entry("A note", "the whole body stays")

    out = render(collection(), [e], max_chars=5000)

    assert "the whole body stays" in out
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_kb_render.py -v`
Expected: `test_a_rule_renders_its_summary_and_not_its_body` FAILS on `assert "y" * 2000 not in out`, and `test_a_rule_without_a_summary_renders_title_only` FAILS on `assert "z" * 900 not in out`. The other two new tests should already PASS - they assert behaviour that must survive, not behaviour being added.

- [ ] **Step 3: Make `_render_entry` kind-aware**

In `src/remem/services/kb.py`, replace `_render_entry`:

```python
def _render_entry(entry: Entry) -> str:
    tags = ", ".join(entry.tags)
    meta = f"_id: {entry.id}_" + (f" _tags: {tags}_" if tags else "")
    return f"### {entry.title}\n\n{_content(entry)}{meta}\n"


def _content(entry: Entry) -> str:
    """What an entry contributes to the block, above its id line.

    A rule contributes its SUMMARY, not its body. Rule bodies here are
    essays - the incident that produced the rule, the reasoning, the
    lesson - and every session was paying for case history that nothing
    reads unless someone asks why. The body is one `recall` away, and the
    id line above is how to reach it.

    A rule with no summary contributes nothing but its title. That is the
    deliberate floor rather than a fallback to the body: the rules written
    before the summary requirement must keep rendering, and rendering
    their bodies is the failure this change exists to fix. Titles here are
    already written as directives ("Run remem ingest from the repository
    root, never a subdirectory"), so a title alone still instructs.

    Deriving a short form from the body was measured and rejected: the
    first paragraph of a rule is the incident, not the instruction.

    Every other kind renders its body unchanged. Notes and docs are not
    injected into every session, so their cost was never the problem.
    """
    if entry.kind is not Kind.RULE:
        return f"{entry.body}\n\n"
    if entry.summary:
        return f"{entry.summary}\n\n"
    return ""
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_kb_render.py -v`
Expected: the four new tests PASS.

Two OLD tests in this file are now expected to FAIL - that is correct and Task 2 fixes them. Do not touch them yet:
`test_rules_are_never_truncated_even_over_budget` and `test_rules_alone_exceeding_the_budget_raises`.

- [ ] **Step 5: Commit**

```bash
git add src/remem/services/kb.py tests/test_kb_render.py
git commit -m "feat: render rules as title and summary, not full bodies"
```

---

### Task 2: Reframe the two invariant tests

**Files:**
- Modify: `tests/test_kb_render.py` (`test_rules_are_never_truncated_even_over_budget` around line 60, `test_rules_alone_exceeding_the_budget_raises` around line 74)

**Interfaces:**
- Consumes: the `entry(..., summary=...)` helper from Task 1; `RulesExceedBudget` from `remem.services.kb`.
- Produces: nothing new.

These two tests encode the invariant the spec keeps: rules are never truncated, and rules that do not fit raise rather than shipping a partial set. Both were written when rules rendered bodies, so both now assert against the wrong text. They are rewritten, **not deleted** - the invariant is unchanged.

- [ ] **Step 1: Rewrite both tests**

In `tests/test_kb_render.py`, replace `test_rules_are_never_truncated_even_over_budget` with:

```python
def test_rules_are_never_truncated_even_over_budget():
    """Rules survive whole while everything else is dropped for space.

    Unchanged invariant, restated for short forms: what must survive whole
    is now each rule's summary, not its body.
    """
    rules = [entry(f"Rule {i}", "body that is not rendered", kind=Kind.RULE,
                   summary="s" * 300) for i in range(4)]
    others = [entry(f"Doc {i}", "z" * 800) for i in range(10)]

    out = render(collection(), rules + others, max_chars=2000)

    for i in range(4):
        assert f"Rule {i}" in out
    assert out.count("s" * 300) == 4     # every rule summary, in full
    assert "z" * 800 not in out          # the budget really did bite
    assert "10 more entries not shown" in out
```

and replace `test_rules_alone_exceeding_the_budget_raises` with:

```python
def test_rules_alone_exceeding_the_budget_raises():
    """The backstop, which short forms make rare rather than remove.

    Rules are cheap now, so the over-budget case needs building rather
    than arriving by accident: ten long summaries against a tiny budget.
    An agent handed a partial rule set proceeds believing it has the
    conventions, which is worse than having none - so this raises.
    """
    rules = [entry(f"Rule {i}", "body", kind=Kind.RULE, summary="y" * 500)
             for i in range(10)]
    with pytest.raises(RulesExceedBudget):
        render(collection(), rules, max_chars=500)
```

- [ ] **Step 2: Run the whole render file**

Run: `uv run pytest tests/test_kb_render.py -v`
Expected: PASS, every test, zero skips.

- [ ] **Step 3: Run the full suite to catch anything else asserting on rule bodies**

Run: `uv run pytest -q`
Expected: PASS. If `tests/test_context_service.py` or `tests/test_hook_context_cli.py` fails, it is asserting a rule body appears in a rendered block; fix it the same way - give the fixture rule a summary and assert on that.

- [ ] **Step 4: Commit**

```bash
git add tests/test_kb_render.py
git commit -m "test: restate the render invariants for rule short forms"
```

---

### Task 3: Require a summary when writing a rule

**Files:**
- Modify: `src/remem/services/write.py` (`remember`, around line 33; exceptions at lines 11-15)
- Modify: `src/remem/cli.py` (`rule`, the `write.remember` call)
- Test: `tests/test_write_rule_summary.py` (create)

**Interfaces:**
- Consumes: `write.remember(...)` as it exists.
- Produces: `write.RuleNeedsSummary(Exception)`, raised by `write.remember` when `kind is Kind.RULE` and `summary` is None or blank. `remember` keeps its signature.

- [ ] **Step 1: Write the failing test**

Create `tests/test_write_rule_summary.py`:

```python
"""A rule must state itself in one line, because that line is what every
session sees. Enforced in the service rather than the CLI: mcp_server's
remember_tool takes a `kind` and would otherwise write a summary-less rule
straight past a CLI-side check."""

from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Kind
from remem.services import write

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def test_a_rule_without_a_summary_is_refused(store, owner):
    with pytest.raises(write.RuleNeedsSummary):
        write.remember(store, owner.id, title="A rule", body="the case for it",
                       kind=Kind.RULE, project="remem")


def test_a_blank_summary_is_refused_too(store, owner):
    """An empty string is not a summary. Accepting it would render a rule
    with a blank line where its instruction should be."""
    with pytest.raises(write.RuleNeedsSummary):
        write.remember(store, owner.id, title="A rule", body="the case",
                       summary="   ", kind=Kind.RULE, project="remem")


def test_a_rule_with_a_summary_is_written(store, owner):
    e = write.remember(store, owner.id, title="A rule", body="the case",
                       summary="do the thing", kind=Kind.RULE, project="remem")
    assert e.summary == "do the thing"


def test_notes_and_docs_do_not_need_a_summary(store, owner):
    """Only rules are injected into every session, so only rules are
    forced to state themselves in a line."""
    for kind in (Kind.NOTE, Kind.DOC):
        e = write.remember(store, owner.id, title=f"A {kind}", body="body",
                           kind=kind, project="remem")
        assert e.summary is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_write_rule_summary.py -v`
Expected: FAIL with `AttributeError: module 'remem.services.write' has no attribute 'RuleNeedsSummary'`.

If instead every test SKIPS, Postgres is not running. Start it with `docker compose up -d` and re-run. A skip is not a pass.

- [ ] **Step 3: Add the exception and the check**

In `src/remem/services/write.py`, add beside the existing exceptions:

```python
class RuleNeedsSummary(Exception):
    """A rule was written without the one line the context block renders.

    Rules are the only kind injected into every session, and since the
    block renders summaries rather than bodies, a rule with no summary
    arrives as a bare title. Refusing at write time is what keeps the
    block improving; the 17 rules that predate this requirement render
    title-only and are backfilled with `remem update --summary`.
    """
```

and at the top of `remember`, before the `Entry(...)` construction:

```python
    if kind is Kind.RULE and not (summary or "").strip():
        # In the service, not the CLI: mcp_server's remember_tool accepts a
        # `kind` and would write a summary-less rule straight past a
        # frontend check. One rule enforced here is one every frontend gets.
        raise RuleNeedsSummary(title)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_write_rule_summary.py -v`
Expected: PASS, 4 tests, zero skips.

- [ ] **Step 5: Make `remem rule` report it as a sentence, not a traceback**

In `src/remem/cli.py`, in the `rule` command, wrap the write the way `update` already wraps `EntryNotFound`:

```python
    with _session() as s:
        try:
            entry = write.remember(
                s.store, s.owner.id, title=title, body=text, kind=Kind.RULE,
                summary=summary,
                project=resolved, tags=list(tag or []), origin=Origin.HUMAN,
            )
        except write.RuleNeedsSummary:
            typer.echo(
                "A rule needs --summary: it is the line every session sees, "
                "since the context block renders summaries rather than bodies.",
                err=True,
            )
            raise typer.Exit(1)
        typer.echo(entry.id)
```

- [ ] **Step 6: Verify the CLI by hand**

Run: `uv run remem rule "A test rule" --body "some body" --project scratch`
Expected: exits 1, prints the sentence above to stderr, no traceback.

Run: `uv run remem rule "A test rule" --body "some body" --summary "do it" --project scratch`
Expected: exits 0, prints an entry id.

Leave the entry that succeeded where it is. remem has no delete command by
design, and this one is on project `scratch`, which no collection resolves - so
it cannot reach any context block. Do not add a delete command to tidy it up.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, zero skips. Existing tests that write rules through `write.remember` without a summary will fail here - fix each by giving it a summary, which is the behaviour now being required.

- [ ] **Step 8: Commit**

```bash
git add src/remem/services/write.py src/remem/cli.py tests/test_write_rule_summary.py
git commit -m "feat: a rule must carry the summary the block renders"
```

---

### Task 4: Teach MCP about summaries

**Files:**
- Modify: `src/remem/mcp_server.py` (`remember_tool`, around line 60)
- Test: `tests/test_mcp_rule_summary.py` (create)

**Interfaces:**
- Consumes: `write.RuleNeedsSummary` from Task 3.
- Produces: `remember_tool(title, body, kind="note", summary=None, project=None, tags=None) -> dict`, returning `{"error": ...}` for a rule with no summary.

Without this task, writing a rule through MCP becomes impossible - Task 3 refuses it and `remember_tool` has no way to supply one.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_rule_summary.py`:

```python
from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate

pytestmark = pytest.mark.db


@pytest.fixture
def env(live_dsn, monkeypatch, tmp_path):
    """remember_tool opens its own session, so the schema must be committed
    before it connects. Same bootstrap as tests/test_mcp_server.py."""
    import psycopg
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))
    return live_dsn


def test_a_rule_without_a_summary_returns_an_error_not_a_raise(env):
    """An MCP tool that raises hands the model a stack trace where a
    sentence would do. Same shape as the invalid-kind response."""
    from remem.mcp_server import remember_tool

    result = remember_tool(title="A rule", body="the case", kind="rule")

    assert "error" in result
    assert "summary" in result["error"]


def test_a_rule_with_a_summary_is_written(env):
    from remem.mcp_server import remember_tool

    result = remember_tool(title="A rule", body="the case", kind="rule",
                           summary="do the thing")

    assert "id" in result


def test_a_note_still_needs_no_summary(env):
    from remem.mcp_server import remember_tool

    assert "id" in remember_tool(title="A note", body="learned something")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_rule_summary.py -v`
Expected: FAIL - `remember_tool() got an unexpected keyword argument 'summary'`.

- [ ] **Step 3: Add the parameter and the error path**

In `src/remem/mcp_server.py`, change `remember_tool`'s signature to:

```python
def remember_tool(
    title: str,
    body: str,
    kind: str = "note",
    summary: str | None = None,
    project: str | None = None,
    tags: list[str] | None = None,
) -> dict:
```

Add to its docstring, after the `kind:` paragraph:

```
    summary: REQUIRED for kind "rule", ignored-if-absent for everything
    else. One line stating the rule itself, because the context block
    injected into every session renders this and not the body. Put the
    case for the rule - the incident, the reasoning - in body, where it
    stays one recall away.
```

and replace the body's write with:

```python
    try:
        parsed_kind = Kind(kind)
    except ValueError:
        return {"error": _invalid_kind_message(kind)}
    with open_session() as s:
        try:
            entry = write.remember(
                s.store, s.owner.id, title=title, body=body, kind=parsed_kind,
                summary=summary,
                project=project or _default_project(),
                tags=list(tags or []), agent=AGENT_NAME,
                session_id=_session_id(), origin=Origin.AGENT,
            )
        except write.RuleNeedsSummary:
            # An error dict, not a raise: a raise reaches the model as a
            # stack trace, and this is a correctable mistake it can retry.
            return {"error": "a rule needs a summary - one line stating the "
                             "rule, which is what every session's context "
                             "block renders instead of the body"}
        return {"id": str(entry.id), "title": entry.title}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_rule_summary.py tests/test_mcp_server.py -v`
Expected: PASS, zero skips. `test_mcp_server.py` must still pass - Step 3 rewrote a function it covers.

- [ ] **Step 5: Commit**

```bash
git add src/remem/mcp_server.py tests/test_mcp_rule_summary.py
git commit -m "feat: recall's sibling learns summaries; MCP can write rules"
```

---

### Task 5: Backfill in place with `update --summary`

**Files:**
- Modify: `src/remem/services/write.py` (`update`, around line 68)
- Modify: `src/remem/cli.py` (`update`, around line 440)
- Test: `tests/test_write_rule_summary.py` (extend)

**Interfaces:**
- Consumes: `write.update(store, owner_id, entry_id, *, title, body, tags, project)`.
- Produces: `write.update(..., summary: str | None = None)` - `None` means leave unchanged, matching every other field on this function.

Without this, putting a summary on one of the 19 existing rules requires `supersede`, which retires the entry, mints a replacement, and - because `summary` feeds the memory file's frontmatter `description` - churns the corresponding memory file. For an editorial addition.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_write_rule_summary.py`:

```python
def test_update_sets_a_summary_without_creating_a_second_entry(store, owner):
    """Backfill is an in-place edit, not a correction.

    supersede would retire the entry, mint a replacement, and churn the
    memory file whose frontmatter description this field feeds - all for
    adding a line that was always meant to be there.
    """
    e = write.remember(store, owner.id, title="A rule", body="the case",
                       summary="first", kind=Kind.RULE, project="remem")

    updated = write.update(store, owner.id, e.id, summary="better line")

    assert updated.id == e.id
    assert updated.summary == "better line"
    assert updated.superseded_by is None


def test_update_leaves_the_summary_alone_when_not_given(store, owner):
    """None means unchanged here, as it does for every other field on
    update. There is no --clear-summary: a wrong summary is fixed by
    writing a better one."""
    e = write.remember(store, owner.id, title="A rule", body="the case",
                       summary="keep me", kind=Kind.RULE, project="remem")

    updated = write.update(store, owner.id, e.id, title="A renamed rule")

    assert updated.summary == "keep me"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_write_rule_summary.py -v`
Expected: FAIL - `update() got an unexpected keyword argument 'summary'`.

- [ ] **Step 3: Add `summary` to `write.update`**

In `src/remem/services/write.py`, change `update`'s signature and body:

```python
def update(
    store: Store,
    owner_id: UUID,
    entry_id: UUID,
    *,
    title: str | None = None,
    body: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
    project: str | None | _Clear = None,
) -> Entry:
    """Change only the fields given. Pass `CLEAR` to null a field out."""
    entry = _require(store, owner_id, entry_id)
    if title is not None:
        entry.title = title
    if body is not None:
        entry.body = body
    if summary is not None:
        # No CLEAR for summary. A wrong summary is fixed by writing a
        # better one, and a rule with none renders title-only rather than
        # breaking - so emptying one has no use case worth the sentinel.
        entry.summary = summary
    if tags is not None:
        entry.tags = list(tags)
    if project is CLEAR:
        entry.project = None
    elif project is not None:
        entry.project = project
    return store.put_entry(entry)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_write_rule_summary.py -v`
Expected: PASS, 6 tests, zero skips.

- [ ] **Step 5: Add `--summary` to `remem update`**

In `src/remem/cli.py`, add the option to `update`'s signature after `--body`:

```python
    summary: Annotated[Optional[str], typer.Option("--summary")] = None,
```

and pass it through in the `write.update` call:

```python
            entry = write.update(s.store, s.owner.id, parsed,
                                 title=title, body=text, summary=summary,
                                 project=new_project, tags=new_tags)
```

Add to the command's docstring, after the existing "Only the fields you pass change." line:

```
    --summary is how a rule written before summaries were required gets
    the line the context block renders, without superseding it.
```

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, zero skips.

- [ ] **Step 7: Commit**

```bash
git add src/remem/services/write.py src/remem/cli.py tests/test_write_rule_summary.py
git commit -m "feat: update --summary, so backfill need not supersede"
```

---

### Task 6: Bring the budget down, correct the stale comment, document it

**Files:**
- Modify: `src/remem/domain.py` (the `Entry.summary` comment, around line 91)
- Modify: `CLAUDE.md`
- Config: `REMEM_MAX_CHARS`

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: nothing new.

- [ ] **Step 1: Correct the `Entry.summary` comment**

In `src/remem/domain.py`, replace the comment above `summary`:

```python
    #: One line stating what this entry is. Two readers: the `description`
    #: field of a Claude Code memory file's frontmatter, which is where it
    #: shipped, and - since 2026-09-04 - the context block, which renders
    #: it INSTEAD OF the body for a rule. Required on rules for that
    #: reason (see services/write.RuleNeedsSummary); nullable everywhere
    #: else, and a rule that predates the requirement renders title-only.
    summary: str | None = None
```

- [ ] **Step 2: Measure the real block, which is the point of the whole change**

Run:

```bash
uv run python -c "
from remem.session import open_session
from remem.services import kb
with open_session() as s:
    c = kb.get(s.store, s.owner.id, 'remem')
    entries = kb.resolve(s.store, s.owner.id, 'remem')
    print(len(kb.render(c, entries, max_chars=100000)), 'chars')
"
```

Expected: well under 8,000 - roughly 2,300 to 2,500, since 17 of the 19 rules render title-only today. If it is over 8,000, stop and report rather than raising the budget to fit: that would be the exact move this change exists to replace.

- [ ] **Step 3: Bring the budget down**

Run: `remem config set REMEM_MAX_CHARS 8000`

Expected: prints the new value and the path of the backup it took.

- [ ] **Step 4: Verify injection end to end**

Run:

```bash
REMEM_HOOK_DEBUG=1 remem hook context --agent claude-code <<< '{"cwd":"/Users/brandon/llmworkspace/remem"}' | head -20
```

Expected: a `# remem` block containing rule titles, no rule bodies, nothing on stderr. Confirm a specific title is present and its body is not - for example the title "A test marker describes what a test does, not whether it still passes" should appear while the phrase "the opencode-adapter branch" should not.

- [ ] **Step 5: Document it in CLAUDE.md**

In `CLAUDE.md`, find the "### Handoffs" section and add this one before it:

```markdown
### What the context block carries

Rules render as **title plus summary**, never their bodies. Rule bodies here
are essays - the incident that produced the rule, the reasoning, the lesson -
and shipping all of them is what made the block outgrow `REMEM_MAX_CHARS`
twice in three days (18,249 chars on 2026-09-02, 25,008 by 2026-09-04). The
body stays one `recall` away and the rendered `_id:` line is how to reach it.

A rule with no summary renders **title only**. That is the floor, not a
fallback to the body: rules written before the requirement must keep
rendering, and rendering their bodies is the failure being fixed. Backfill one
with `remem update --summary`, which edits in place - `supersede` would mint a
replacement and churn the memory file whose frontmatter `description` this
same field feeds.

Deriving the short form from the body was measured and rejected: a rule's
first paragraph is the incident, not the instruction.

`services/write.remember` raises `RuleNeedsSummary` for a rule without one.
That check is in the service and not in `cli.py` because `mcp_server`'s
`remember_tool` takes a `kind` and would otherwise write a summary-less rule
straight past a frontend check.

The never-truncate invariant is unchanged: `RulesExceedBudget` still raises
rather than shipping a partial rule set, because an agent given part of the
conventions proceeds believing it has all of them. Short forms make that
exception rare; they do not soften it.
```

- [ ] **Step 6: Run the full suite one last time**

Run: `uv run pytest -q`
Expected: PASS, zero skips.

- [ ] **Step 7: Commit**

```bash
git add src/remem/domain.py CLAUDE.md
git commit -m "docs: the block carries the rule, the entry keeps the case"
```
