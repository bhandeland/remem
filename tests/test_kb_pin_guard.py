"""`kb.pin` must not discard the store's ownership verdict.

`PostgresStore.pin` returns False when its ownership guards match nothing,
"rather than writing nothing silently" - the same contract
`store.set_superseded` carries, and which `write.supersede` already checks
and raises on. `kb.pin` threw the answer away, so a pin that wrote no row
reported success to `remem kb pin` and to the MCP `kb_pin` tool.

The Protocol is what hid it: `Store.pin` was declared `-> None`, so no
reader of the interface could see there was a verdict to check.
"""

from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import CollectionQuery, Kind, Origin
from remem.services import kb
from remem.services.write import remember

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def test_pin_raises_when_the_store_reports_it_wrote_nothing(
    store, owner, monkeypatch
):
    """A pin that matched no rows is a failure, not a silent success.

    Reaching this through real data is not possible from the service - it
    checks the collection and the entry under this owner first - so the
    False is injected. That is the point: the guard exists for the race
    between those reads and the write, which is exactly the case
    `write.supersede` documents and raises on.
    """
    kb.create(
        store, owner.id, slug="kb", title="KB", project="proj",
        query=CollectionQuery(project="proj"),
    )
    entry = remember(
        store, owner.id, title="A fact", body="body\n", kind=Kind.NOTE,
        project="proj", origin=Origin.HUMAN,
    )
    monkeypatch.setattr(store, "pin", lambda *a, **k: False)

    with pytest.raises(RuntimeError):
        kb.pin(store, owner.id, "kb", entry.id)
