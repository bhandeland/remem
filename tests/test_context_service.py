"""The context block, once, for every harness.

Injection used to live inside the Claude Code hook, which meant a second
harness could record events but could not be told anything.
"""

from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import CollectionQuery
from remem.services import context, handoff, kb
from remem.services.write import remember

pytestmark = pytest.mark.db

BODY = "## Done\nx\n\n## In flight\n\n## Next steps\n\n## Gotchas\n"


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def test_block_renders_a_knowledge_base(store, owner):
    kb.create(store, owner.id, slug="demo", title="Demo",
              query=CollectionQuery(tags=["style"]))
    remember(store, owner.id, title="A rule", body="Body text", tags=["style"])

    block = context.block(store, owner.id, "demo", max_chars=10_000)

    assert "A rule" in block


def test_block_is_empty_for_a_project_with_no_knowledge_base(store, owner):
    """Silence, not an exception. Every caller of this is fail-soft."""
    assert context.block(store, owner.id, "nothing-here", max_chars=10_000) == ""


def test_the_reason_for_an_empty_block_is_reported(store, owner):
    """Silence is ambiguous, which is why REMEM_HOOK_DEBUG exists. The
    service knows why it returned nothing; only the frontend knows where to
    say so, hence the callable."""
    said: list[str] = []

    context.block(store, owner.id, "nothing-here", max_chars=10_000, note=said.append)

    assert any("nothing-here" in line for line in said)


def test_the_reason_names_the_principal_when_the_caller_provides_it(store, owner):
    """A wrong or unexpected REMEM_USER_ID is one of the likeliest reasons
    for silent injection, so the diagnostic should name the principal it
    looked under - when the caller has one to give. The service has no
    handle of its own; only owner_id, which is not human-readable."""
    said: list[str] = []

    context.block(
        store, owner.id, "nothing-here", max_chars=10_000,
        note=said.append, owner_handle="brandon",
    )

    assert any("brandon" in line for line in said)


def test_the_handoff_pointer_is_appended_outside_max_chars(store, owner):
    """The pointer is a fixed ~20 tokens appended after render, so it must
    survive even a max_chars budget too small to hold the knowledge base
    itself - competing with rules for that budget would be absurd."""
    kb.create(store, owner.id, slug="demo", title="Demo",
              query=CollectionQuery(tags=["style"]))
    remember(store, owner.id, title="A rule", body="Body text", tags=["style"])
    handoff.write(store, owner.id, project="demo", topic="ci", body=BODY)

    # Just enough budget for the header, none for the entry: the knowledge
    # base renders (nearly) empty, but the pointer must still show up.
    block = context.block(store, owner.id, "demo", max_chars=len("# Demo\n"))

    assert "A rule" not in block
    assert "Handoff available: ci" in block
    assert "remem-prime ci" in block
