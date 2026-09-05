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
