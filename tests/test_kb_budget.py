"""The numeric half of the budget question, for callers that must ask it.

`budget_advisories` answers "what should I tell a person?" and is silent
about a healthy knowledge base by design - there is nothing to say. That
makes it useless to anything wanting to *display* the number continuously,
which is what a status line does: below the warning fraction it emits no
line at all, so a consumer parsing its prose can only ever render the two
unhealthy states and never the healthy one it spends most of its life in.

So the quantity gets a typed surface. The point of this file is that the
surface and the advisory can never disagree: both route through
`classify`, and the agreement is asserted rather than assumed, because two
copies of a threshold comparison drifting apart is precisely the failure
`rules_chars` and the raise site already share `_header_and_rules` to
avoid.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import CollectionQuery, Kind
from saddlebag.services import kb
from saddlebag.services.write import remember

# ---------------- the thresholds, measured without a database ----------------


def test_a_knowledge_base_well_inside_the_budget_is_ok() -> None:
    assert kb.classify(100, 1000) is kb.BudgetState.OK


def test_the_warning_fires_exactly_at_the_fraction_not_past_it() -> None:
    """The boundary is `>=`, matching the advisory's own comparison.

    Asserted rather than left to reading because an off-by-one here is
    invisible: it merely delays the warning the whole feature exists to
    deliver, and nothing else changes.
    """
    at = int(1000 * kb.BUDGET_WARN_FRACTION)

    assert kb.classify(at - 1, 1000) is kb.BudgetState.OK
    assert kb.classify(at, 1000) is kb.BudgetState.WARN


def test_filling_the_budget_exactly_is_still_only_a_warning() -> None:
    """`render` raises on `used > max_chars`, so equality still injects.

    Reporting OVER here would tell a user injection is dead in a session
    where it is in fact working, which is a worse lie than the silence
    this command replaces.
    """
    assert kb.classify(1000, 1000) is kb.BudgetState.WARN
    assert kb.classify(1001, 1000) is kb.BudgetState.OVER


def test_the_fraction_is_reported_for_the_caller_that_draws_a_bar() -> None:
    b = kb.Budget(slug="s", used=500, budget=1000, state=kb.BudgetState.OK)
    assert b.fraction == 0.5


def test_a_zero_budget_does_not_divide_by_zero() -> None:
    """`max_chars` is read from config and a user can set it to anything.

    A status line asking for the fraction must not be the thing that
    raises - the whole consumer is a fail-soft widget.
    """
    b = kb.Budget(slug="s", used=10, budget=0, state=kb.BudgetState.OVER)
    assert b.fraction == 0.0


# ---------------- the lookup, which needs a store ----------------


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner_id(store: PostgresStore) -> UUID:
    return store.ensure_principal("kb-budget-cmd").id


def _kb_with(
    store: PostgresStore,
    owner_id: UUID,
    slug: str,
    *,
    rules: int = 0,
    rule_summary: str = "short",
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


@pytest.mark.db
def test_budget_reports_the_same_quantity_the_advisory_measures(
    store: PostgresStore, owner_id: UUID
) -> None:
    _kb_with(store, owner_id, "calm", rules=2)

    got = kb.budget(store, owner_id, "calm", max_chars=20000)

    assert got.slug == "calm"
    assert got.budget == 20000
    assert got.used == kb.rules_chars(
        kb.get(store, owner_id, "calm"), kb.resolve(store, owner_id, "calm")
    )


@pytest.mark.db
def test_an_unknown_knowledge_base_is_not_reported_as_empty(
    store: PostgresStore, owner_id: UUID
) -> None:
    """Zero characters and "no such knowledge base" are different answers.

    Returning a healthy-looking zero for a slug that does not exist would
    render a status line reading 0% for a project whose context block is
    missing entirely - the exact confusion this command exists to remove.
    """
    with pytest.raises(kb.CollectionNotFound):
        kb.budget(store, owner_id, "nonexistent", max_chars=1000)


@pytest.mark.db
def test_the_command_and_the_advisory_never_disagree(
    store: PostgresStore, owner_id: UUID
) -> None:
    """The invariant this file exists for.

    A knowledge base the advisory stays silent about must classify OK, and
    one it names must not. If these drift, a status line reports healthy
    while `bag record status` reports dead, and a user believes whichever
    they read last.
    """
    budget_chars = 800
    _kb_with(store, owner_id, "fat", rules=8, rule_summary="x" * 300)
    _kb_with(store, owner_id, "fine", rules=1)

    lines = kb.budget_advisories(store, owner_id, max_chars=budget_chars)
    named = {slug for slug in ("fat", "fine") if any(slug in ln for ln in lines)}

    for slug in ("fat", "fine"):
        state = kb.budget(store, owner_id, slug, max_chars=budget_chars).state
        assert (state is not kb.BudgetState.OK) == (slug in named), slug


@pytest.mark.db
def test_every_state_serialises_for_a_machine_consumer(
    store: PostgresStore, owner_id: UUID
) -> None:
    _kb_with(store, owner_id, "fat", rules=8, rule_summary="x" * 300)

    payload = kb.budget_to_dict(kb.budget(store, owner_id, "fat", max_chars=800))

    assert payload["slug"] == "fat"
    assert payload["state"] == "over"
    assert payload["budget"] == 800
    assert isinstance(payload["used"], int)
    assert 0.0 <= payload["fraction"]
