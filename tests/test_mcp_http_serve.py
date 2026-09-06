from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def reset_module_state():
    """mcp_server keeps launch configuration in module globals, so a test that
    sets them would otherwise leak into every test that runs after it."""
    from remem import mcp_server

    yield
    mcp_server.configure(project=None, http=False)


def test_stdio_derives_the_project_from_the_working_directory(monkeypatch):
    from remem import mcp_server

    monkeypatch.setattr(mcp_server, "resolve_project", lambda: "from-cwd")
    assert mcp_server._default_project() == "from-cwd"


def test_a_pinned_project_replaces_the_working_directory(monkeypatch):
    from remem import mcp_server

    monkeypatch.setattr(mcp_server, "resolve_project", lambda: "from-cwd")
    mcp_server.configure(project="saddle", http=True)
    assert mcp_server._default_project() == "saddle"


def test_stdio_reports_the_session_id_from_the_environment(monkeypatch):
    from remem import mcp_server

    monkeypatch.setenv("CLAUDE_SESSION_ID", "abc123")
    assert mcp_server._session_id() == "abc123"


def test_http_reports_no_session_id_even_when_the_environment_sets_one(monkeypatch):
    # The server's environment belongs to whatever launched it, not to the
    # agent making the call, so the value is actively wrong rather than
    # merely absent. Recording null is the honest answer.
    from remem import mcp_server

    monkeypatch.setenv("CLAUDE_SESSION_ID", "the-launchers-session")
    mcp_server.configure(http=True)
    assert mcp_server._session_id() is None
