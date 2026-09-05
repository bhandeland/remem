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
