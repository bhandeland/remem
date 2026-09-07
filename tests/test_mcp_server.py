import pytest

from remem.backends.postgres.migrate import migrate
from tests.conftest import scalar

pytestmark = pytest.mark.db


@pytest.fixture
def env(live_dsn, monkeypatch, tmp_path):
    import psycopg

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))
    return live_dsn


def test_remember_then_recall(env):
    from remem.mcp_server import recall_tool, remember_tool

    result = remember_tool(title="Postgres tuning", body="raise work_mem")
    assert "id" in result

    hits = recall_tool(query="work_mem")
    assert hits[0]["title"] == "Postgres tuning"
    assert "snippet" in hits[0]


def test_recall_returns_an_empty_list_when_nothing_matches(env):
    from remem.mcp_server import recall_tool

    assert recall_tool(query="zzzz-no-match-zzzz") == []


def test_get_entry_returns_the_full_body(env):
    from remem.mcp_server import get_entry_tool, remember_tool

    created = remember_tool(title="T", body="the complete body")
    assert get_entry_tool(entry_id=created["id"])["body"] == "the complete body"


def test_get_entry_reports_a_missing_id_without_raising(env):
    from remem.domain import new_id
    from remem.mcp_server import get_entry_tool

    assert "error" in get_entry_tool(entry_id=str(new_id()))


def test_supersede_hides_the_old_entry_from_recall(env):
    from remem.mcp_server import recall_tool, remember_tool, supersede_tool

    old = remember_tool(title="Fridays", body="deploy fridays")
    supersede_tool(entry_id=old["id"], title="Tuesdays", body="deploy tuesdays")
    assert [h["title"] for h in recall_tool(query="deploy")] == ["Tuesdays"]


def _legacy_rule(dsn, title):
    """A rule with no summary - the state the 17 pre-existing rules on this
    machine are in, and which write.remember (what remember_tool calls)
    can no longer produce. Written directly through the store, the same
    way tests/test_cli.py's helper of the same name does."""
    import psycopg

    from remem.backends.postgres.store import PostgresStore
    from remem.domain import Entry, Kind, Origin, new_id

    with psycopg.connect(dsn) as c:
        store = PostgresStore(c)
        owner = store.ensure_principal("brandon")
        entry = store.put_entry(
            Entry(
                id=new_id(),
                kind=Kind.RULE,
                title=title,
                body="the case",
                owner_id=owner.id,
                origin=Origin.HUMAN,
            )
        )
        c.commit()
    return str(entry.id)


def test_supersede_a_legacy_rule_without_a_summary_reports_an_error(env):
    from remem.mcp_server import supersede_tool

    entry_id = _legacy_rule(env, "A legacy rule")
    result = supersede_tool(
        entry_id=entry_id, title="Corrected", body="the corrected case"
    )
    assert "error" in result
    assert "summary" in result["error"]


def test_supersede_a_legacy_rule_with_a_summary_succeeds(env):
    from remem.mcp_server import supersede_tool

    entry_id = _legacy_rule(env, "Another legacy rule")
    result = supersede_tool(
        entry_id=entry_id,
        title="Corrected",
        body="the corrected case",
        summary="state the rule in one line",
    )
    assert "id" in result
    # supersede_tool's return dict carries no summary - read it back
    # straight from the store, the same way test_cli.py's _summary_of does.
    import psycopg

    with psycopg.connect(env) as c:
        summary = scalar(
            c.execute("select summary from entries where id = %s", (result["id"],))
        )
    assert summary == "state the rule in one line"


def test_kb_list_and_context(env):
    from remem.mcp_server import (
        kb_context_tool,
        kb_list_tool,
        kb_pin_tool,
        remember_tool,
    )
    from remem.services import kb
    from remem.session import open_session

    with open_session() as s:
        kb.create(s.store, s.owner.id, slug="core", title="Core")
        s.conn.commit()

    assert any(c["slug"] == "core" for c in kb_list_tool())

    entry = remember_tool(
        title="A rule", body="always lint", summary="Run lint", kind="rule"
    )
    kb_pin_tool(slug="core", entry_id=entry["id"])

    block = kb_context_tool(slug="core")
    assert "Run lint" in block
    assert "## Rules" in block


def test_kb_context_for_an_unknown_slug_returns_a_message_not_an_exception(env):
    from remem.mcp_server import kb_context_tool

    assert "core" not in kb_context_tool(slug="nope")


def test_kb_pin_reports_a_nonexistent_entry_without_raising(env):
    from remem.domain import new_id
    from remem.mcp_server import kb_pin_tool
    from remem.services import kb
    from remem.session import open_session

    with open_session() as s:
        kb.create(s.store, s.owner.id, slug="core", title="Core")
        s.conn.commit()

    result = kb_pin_tool(slug="core", entry_id=str(new_id()))
    assert "error" in result


def test_remember_reports_an_invalid_kind_without_raising(env):
    from remem.mcp_server import remember_tool

    result = remember_tool(title="T", body="B", kind="bogus")
    assert "error" in result


def test_recall_reports_an_invalid_kind_without_raising(env):
    from remem.mcp_server import recall_tool

    result = recall_tool(query="anything", kind="bogus")
    assert "error" in result


def test_tools_are_registered_with_the_server(env):
    import asyncio

    from remem.mcp_server import mcp

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {
        "remember",
        "recall",
        "get_entry",
        "supersede",
        "kb_context",
        "kb_list",
        "kb_pin",
    }
