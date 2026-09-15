from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import Entry, Kind, Principal, Query, new_id

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


def add(
    store: PostgresStore, owner: Principal, title: str, body: str, **kw: Any
) -> Entry:
    e = Entry(
        id=new_id(),
        kind=kw.pop("kind", Kind.NOTE),
        title=title,
        body=body,
        owner_id=owner.id,
        **kw,
    )
    return store.put_entry(e)


def test_finds_by_body_text(store: PostgresStore, owner: Principal) -> None:
    add(store, owner, "Postgres tuning", "raise work_mem for large sorts")
    add(store, owner, "Unrelated", "nothing to see")
    hits = store.search(Query(text="work_mem"), owner.id)
    assert [h.entry.title for h in hits] == ["Postgres tuning"]


def test_title_outranks_body(store: PostgresStore, owner: Principal) -> None:
    add(store, owner, "Indexing", "unrelated filler text")
    add(store, owner, "Filler", "this mentions indexing once")
    hits = store.search(Query(text="indexing"), owner.id)
    assert hits[0].entry.title == "Indexing"
    assert hits[0].rank >= hits[1].rank


def test_returns_a_snippet(store: PostgresStore, owner: Principal) -> None:
    add(store, owner, "Deploys", "always run migrations before restarting workers")
    hits = store.search(Query(text="migrations"), owner.id)
    assert "migrations" in hits[0].snippet.lower()


def test_excludes_superseded_by_default(store: PostgresStore, owner: Principal) -> None:
    old = add(store, owner, "Old truth", "we deploy on fridays")
    new = add(store, owner, "New truth", "we deploy on tuesdays")
    store.set_superseded(old.id, new.id, owner.id)
    titles = [h.entry.title for h in store.search(Query(text="deploy"), owner.id)]
    assert titles == ["New truth"]


def test_include_superseded_opt_in(store: PostgresStore, owner: Principal) -> None:
    old = add(store, owner, "Old truth", "we deploy on fridays")
    new = add(store, owner, "New truth", "we deploy on tuesdays")
    store.set_superseded(old.id, new.id, owner.id)
    hits = store.search(Query(text="deploy", include_superseded=True), owner.id)
    assert len(hits) == 2


def test_never_returns_another_owners_entries(
    store: PostgresStore, owner: Principal
) -> None:
    other = store.ensure_principal("someone-else")
    add(store, owner, "Mine", "secret sauce")
    e = Entry(
        id=new_id(),
        kind=Kind.NOTE,
        title="Theirs",
        body="secret sauce",
        owner_id=other.id,
    )
    store.put_entry(e)
    hits = store.search(Query(text="secret"), owner.id)
    assert [h.entry.title for h in hits] == ["Mine"]


def test_filters_by_kind(store: PostgresStore, owner: Principal) -> None:
    add(store, owner, "A rule", "always lint", kind=Kind.RULE)
    add(store, owner, "A memory", "always lint", kind=Kind.NOTE)
    hits = store.search(Query(text="lint", kinds=[Kind.RULE]), owner.id)
    assert [h.entry.title for h in hits] == ["A rule"]


def test_filters_by_project(store: PostgresStore, owner: Principal) -> None:
    add(store, owner, "In project", "shared word", project="saddlebag")
    add(store, owner, "Elsewhere", "shared word", project="other")
    hits = store.search(Query(text="shared", project="saddlebag"), owner.id)
    assert [h.entry.title for h in hits] == ["In project"]


def test_filters_by_tags_overlap(store: PostgresStore, owner: Principal) -> None:
    add(store, owner, "Tagged", "shared word", tags=["style", "sql"])
    add(store, owner, "Untagged", "shared word", tags=["other"])
    hits = store.search(Query(text="shared", tags=["sql"]), owner.id)
    assert [h.entry.title for h in hits] == ["Tagged"]


def test_since_filter(store: PostgresStore, owner: Principal) -> None:
    add(store, owner, "Recent", "shared word")
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert store.search(Query(text="shared", since=future), owner.id) == []


def test_no_text_degrades_to_a_listing_newest_first(
    store: PostgresStore, owner: Principal
) -> None:
    add(store, owner, "First", "a")
    add(store, owner, "Second", "b")
    titles = [h.entry.title for h in store.search(Query(), owner.id)]
    assert titles == ["Second", "First"]


def test_limit_is_respected(store: PostgresStore, owner: Principal) -> None:
    for i in range(5):
        add(store, owner, f"Entry {i}", "shared word")
    assert len(store.search(Query(text="shared", limit=2), owner.id)) == 2


def test_unparseable_query_text_does_not_raise(
    store: PostgresStore, owner: Principal
) -> None:
    add(store, owner, "Anything", "some body text")
    # to_tsquery would raise on all of these; websearch_to_tsquery must not.
    for bad in ["", "   ", ":::", "a & | b", '"unclosed', "!!!"]:
        store.search(Query(text=bad), owner.id)


def test_tags_are_searchable_text(store: PostgresStore, owner: Principal) -> None:
    add(store, owner, "Untitled", "nothing relevant here", tags=["kubernetes"])
    hits = store.search(Query(text="kubernetes"), owner.id)
    assert [h.entry.title for h in hits] == ["Untitled"]


def test_snippet_is_plain_text_without_markup(
    store: PostgresStore, owner: Principal
) -> None:
    add(store, owner, "Deploys", "Run migrations before restarting workers.")
    hits = store.search(Query(text="migrations"), owner.id)
    assert "<b>" not in hits[0].snippet
    assert "</b>" not in hits[0].snippet
    assert "migrations" in hits[0].snippet
