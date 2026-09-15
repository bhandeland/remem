"""The one place a silently-dead context block becomes visible.

Every injection path is fail-soft: `kb.RulesExceedBudget` escapes into a
hook that exits 0 and prints nothing, so a knowledge base whose rules have
outgrown `BAG_MAX_CHARS` stops being injected on every harness with no
output anywhere. These tests pin the advisory that says so, and - the point
of the file - pin the *quantity* it measures.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import Collection, CollectionQuery, Entry, Kind, new_id
from saddlebag.services import kb
from saddlebag.services.write import remember

OWNER = new_id()


def collection(**kw: Any) -> Collection:
    return Collection(
        id=new_id(),
        slug=kw.pop("slug", "s"),
        title=kw.pop("title", "My KB"),
        owner_id=OWNER,
        **kw,
    )


def entry(title: str, body: str, kind: Kind = Kind.NOTE, summary: str = "") -> Entry:
    return Entry(
        id=new_id(),
        kind=kind,
        title=title,
        body=body,
        owner_id=OWNER,
        summary=summary or None,
    )


# ---------------- the quantity, measured without a database ----------------


def test_rules_chars_is_exactly_the_number_the_raise_site_reports() -> None:
    """The check and the failure must count the same characters.

    If these two ever drift, the advisory reports healthy on a knowledge
    base that is raising, which is the entire failure this feature exists
    to end. The raise message names its number, so the test can read it
    back rather than recomputing it a third way.
    """
    c = collection(description="a description that costs characters too")
    rules = [entry(f"Rule {i}", "body", Kind.RULE, summary="x" * 200) for i in range(5)]

    with pytest.raises(kb.RulesExceedBudget) as excinfo:
        kb.render(c, rules, max_chars=200)

    reported = re.search(r"need (\d+) chars", str(excinfo.value))
    assert reported is not None, "the raise site stopped naming its number"
    assert kb.rules_chars(c, rules) == int(reported.group(1))


def test_rules_chars_ignores_everything_that_is_not_a_rule() -> None:
    """Notes are dropped whole to make room; rules never are.

    So notes cannot cause the failure and must not count toward the
    warning either - a knowledge base with a thousand notes and two rules
    is healthy, it is merely crowded.
    """
    c = collection()
    rules = [entry("Rule", "body", Kind.RULE, summary="s" * 100)]
    with_notes = rules + [entry(f"Note {i}", "n" * 5000) for i in range(20)]

    assert kb.rules_chars(c, with_notes) == kb.rules_chars(c, rules)


# ---------------- the sweep, which needs a store ----------------


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner_id(store: PostgresStore) -> UUID:
    return store.ensure_principal("kb-budget").id


def _kb_with(
    store: PostgresStore,
    owner_id: UUID,
    slug: str,
    *,
    rules: int = 0,
    rule_summary: str = "short",
    notes: int = 0,
    note_body: str = "short",
) -> None:
    kb.create(
        store,
        owner_id,
        slug=slug,
        title=slug,
        project=slug,
        query=CollectionQuery(project=slug),
    )
    for i in range(rules):
        remember(
            store,
            owner_id,
            title=f"{slug} rule {i}",
            body="the incident, at length",
            kind=Kind.RULE,
            project=slug,
            summary=rule_summary,
        )
    for i in range(notes):
        remember(
            store,
            owner_id,
            title=f"{slug} note {i}",
            body=note_body,
            kind=Kind.NOTE,
            project=slug,
        )


@pytest.mark.db
def test_a_knowledge_base_under_budget_says_nothing(
    store: PostgresStore, owner_id: UUID
) -> None:
    _kb_with(store, owner_id, "calm", rules=2, notes=2)
    assert kb.budget_advisories(store, owner_id, max_chars=20000) == []


@pytest.mark.db
def test_rules_over_the_budget_are_reported_as_injection_being_dead(
    store: PostgresStore, owner_id: UUID
) -> None:
    """And note what a length-based check would have to measure here.

    `render` raises, so nothing is injected at all - the string that ships
    is "". Anything that asks "how big is the block?" is handed a number
    comfortably under the budget for a knowledge base that has stopped
    working entirely.
    """
    budget = 800
    _kb_with(store, owner_id, "fat", rules=8, rule_summary="x" * 300)

    with pytest.raises(kb.RulesExceedBudget):
        kb.render(
            kb.get(store, owner_id, "fat"),
            kb.resolve(store, owner_id, "fat"),
            budget,
        )

    lines = kb.budget_advisories(store, owner_id, max_chars=budget)

    assert len(lines) == 1
    assert "fat" in lines[0]
    assert "not being injected" in lines[0]
    assert "bag kb show fat" in lines[0]


@pytest.mark.db
def test_notes_crowded_out_of_a_full_block_are_not_a_budget_problem(
    store: PostgresStore, owner_id: UUID
) -> None:
    """The regression guard that matters, in the direction it can be shown.

    Rules never truncate and notes are dropped whole to make room, so the
    block that ships is pinned at the budget by construction: it can never
    measure over it, and it measures *at* it for any knowledge base with
    more notes than fit. A check written against the rendered block's
    length therefore fires on this one - one small rule, a pile of notes,
    nothing wrong - while saying nothing at all about the knowledge base in
    the test above, whose block is zero characters because it raised.
    The length of the block is uncorrelated with the failure in both
    directions; rules plus header is the only quantity that predicts it.
    """
    budget = 2000
    _kb_with(
        store,
        owner_id,
        "crowded",
        rules=1,
        rule_summary="x" * 100,
        notes=20,
        note_body="n" * 200,
    )

    rendered = kb.render(
        kb.get(store, owner_id, "crowded"),
        kb.resolve(store, owner_id, "crowded"),
        budget,
    )
    # A length-based check sees a block filled to the brim...
    assert len(rendered) > budget * kb.BUDGET_WARN_FRACTION
    # ...with notes it silently dropped to get there.
    assert "not shown" in rendered

    # The rules, which are the only thing that can ever kill injection, are
    # nowhere near the budget - so there is nothing to say.
    assert kb.budget_advisories(store, owner_id, max_chars=budget) == []


@pytest.mark.db
def test_a_knowledge_base_approaching_the_budget_is_warned_before_it_dies(
    store: PostgresStore, owner_id: UUID
) -> None:
    """The value is a warning before injection dies, not a post-mortem."""
    _kb_with(store, owner_id, "nearly", rules=3, rule_summary="x" * 300)
    size = kb.rules_chars(
        kb.get(store, owner_id, "nearly"), kb.resolve(store, owner_id, "nearly")
    )
    # Comfortably inside the budget, comfortably past the warning fraction.
    budget = int(size / ((1 + kb.BUDGET_WARN_FRACTION) / 2))
    assert size < budget

    lines = kb.budget_advisories(store, owner_id, max_chars=budget)

    assert len(lines) == 1
    assert "nearly" in lines[0]
    assert "not being injected" not in lines[0]
    assert str(budget) in lines[0]


@pytest.mark.db
def test_every_unhealthy_knowledge_base_gets_its_own_line(
    store: PostgresStore, owner_id: UUID
) -> None:
    _kb_with(store, owner_id, "one", rules=8, rule_summary="x" * 300)
    _kb_with(store, owner_id, "two", rules=8, rule_summary="x" * 300)
    _kb_with(store, owner_id, "fine", rules=1)

    lines = kb.budget_advisories(store, owner_id, max_chars=800)

    assert len(lines) == 2
    assert {"one" in line for line in lines} == {True, False}
