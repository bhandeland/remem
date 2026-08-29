"""Typo-tolerant search.

Full-text search matches lexemes, so a misspelled query matches nothing. These
tests pin the fallback behaviour: fuzzy matching runs ONLY when exact search
comes back empty, and a fuzzy hit is always labelled as one so a caller never
mistakes an approximate match for a certain one.
"""

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Kind, Match, Query
from remem.services.search import find
from remem.services.write import remember

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def _indexdef(conn, name):
    row = conn.execute(
        "select indexdef from pg_indexes where indexname = %s", (name,)
    ).fetchone()
    return row[0] if row else None


# --- schema -----------------------------------------------------------------


def test_pg_trgm_is_installed(store, conn):
    assert conn.execute(
        "select 1 from pg_extension where extname = 'pg_trgm'"
    ).fetchone() is not None


def test_trigram_indexes_exist_and_exclude_superseded(store, conn):
    for name in ("entries_title_trgm_idx", "entries_body_trgm_idx"):
        definition = _indexdef(conn, name)
        assert definition is not None, name
        assert "gin_trgm_ops" in definition, name
        assert "superseded_by IS NULL" in definition, name


# --- fallback behaviour -----------------------------------------------------


def test_exact_matches_are_never_fuzzy(store, owner):
    remember(store, owner.id, title="Postgres tuning", body="raise work_mem")
    hits = find(store, owner.id, Query(text="work_mem"))
    assert [h.entry.title for h in hits] == ["Postgres tuning"]
    assert hits[0].match is Match.EXACT


def test_a_typo_in_the_title_still_finds_the_entry(store, owner):
    remember(store, owner.id, title="Postgres connection pooling",
             body="the pool saturates under sustained load")
    hits = find(store, owner.id, Query(text="postgres conection pooling"))
    assert [h.entry.title for h in hits] == ["Postgres connection pooling"]
    assert hits[0].match is Match.FUZZY


def test_a_typo_in_a_body_word_still_finds_the_entry(store, owner):
    remember(store, owner.id, title="Deploy checklist",
             body="always run migrations before restarting the workers")
    hits = find(store, owner.id, Query(text="migratoins"))
    assert [h.entry.title for h in hits] == ["Deploy checklist"]
    assert hits[0].match is Match.FUZZY


def test_fuzzy_never_runs_when_exact_search_found_anything(store, owner):
    """The whole point of fallback: good results are never diluted."""
    remember(store, owner.id, title="Migrations", body="run them first")
    remember(store, owner.id, title="Migratoins typo entry", body="unrelated")
    hits = find(store, owner.id, Query(text="migrations"))
    assert all(h.match is Match.EXACT for h in hits)
    assert "Migrations" in [h.entry.title for h in hits]


def test_nonsense_still_returns_nothing(store, owner):
    remember(store, owner.id, title="Postgres tuning", body="raise work_mem")
    assert find(store, owner.id, Query(text="zzzqqqxxvv")) == []


def test_fuzzy_respects_owner_scoping(store, owner):
    other = store.ensure_principal("someone-else")
    remember(store, other.id, title="Postgres connection pooling", body="theirs")
    assert find(store, owner.id, Query(text="postgres conection pooling")) == []


def test_fuzzy_excludes_superseded_entries(store, owner):
    from remem.services.write import supersede

    old = remember(store, owner.id, title="Postgres connection pooling",
                   body="the old truth")
    supersede(store, owner.id, old.id, title="Pgbouncer pooling",
              body="the new truth")
    hits = find(store, owner.id, Query(text="postgres conection pooling"))
    assert "Postgres connection pooling" not in [h.entry.title for h in hits]


def test_fuzzy_respects_other_filters(store, owner):
    remember(store, owner.id, title="Postgres connection pooling",
             body="x", project="alpha", kind=Kind.RULE)
    assert find(store, owner.id,
                Query(text="postgres conection pooling", project="beta")) == []
    hits = find(store, owner.id,
                Query(text="postgres conection pooling", kinds=[Kind.RULE]))
    assert len(hits) == 1


def test_fuzzy_respects_the_limit(store, owner):
    for i in range(5):
        remember(store, owner.id, title=f"Postgres connection pooling {i}",
                 body="x")
    assert len(find(store, owner.id,
                    Query(text="postgres conection pooling", limit=2))) == 2


def test_a_fuzzy_hit_carries_a_snippet(store, owner):
    remember(store, owner.id, title="Postgres connection pooling",
             body="the pool saturates under sustained load")
    hits = find(store, owner.id, Query(text="postgres conection pooling"))
    assert hits[0].snippet


# --- surfacing to callers ---------------------------------------------------


@pytest.mark.db
def test_cli_marks_fuzzy_results_and_says_so(live_dsn, monkeypatch, tmp_path):
    """A caller must be able to tell an approximate match from an exact one."""
    import json as _json

    import psycopg
    from typer.testing import CliRunner

    from remem.cli import app

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))

    runner = CliRunner()
    runner.invoke(app, ["remember", "Postgres connection pooling",
                        "--body", "the pool saturates under load"])

    exact = runner.invoke(app, ["search", "saturates", "--json"])
    assert _json.loads(exact.stdout)[0]["match"] == "exact"

    fuzzy = runner.invoke(app, ["search", "postgres conection pooling", "--json"])
    payload = _json.loads(fuzzy.stdout)
    assert payload[0]["match"] == "fuzzy"

    human = runner.invoke(app, ["search", "postgres conection pooling"])
    assert "No exact or related matches" in human.stdout
    assert "?" in human.stdout


@pytest.mark.db
def test_mcp_recall_labels_fuzzy_results(live_dsn, monkeypatch, tmp_path):
    import psycopg

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))

    from remem.mcp_server import recall_tool, remember_tool

    remember_tool(title="Postgres connection pooling",
                  body="the pool saturates under load")

    assert recall_tool(query="saturates")[0]["match"] == "exact"
    assert recall_tool(query="postgres conection pooling")[0]["match"] == "fuzzy"
