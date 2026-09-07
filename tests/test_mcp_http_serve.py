from __future__ import annotations

from typing import Any

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


def test_the_listener_allowlists_exactly_the_bound_address():
    # enable_dns_rebinding_protection defaults to True, so an empty or wrong
    # allowed_hosts rejects every request the container makes. This assertion
    # is the difference between the feature working and silently refusing.
    from remem.mcp_server import http_run_kwargs

    kw = http_run_kwargs("192.168.64.3", 9100)
    assert kw["host"] == "192.168.64.3"
    assert kw["port"] == 9100
    assert kw["transport_security"].allowed_hosts == ["192.168.64.3:9100"]
    assert kw["transport_security"].allowed_origins == []
    assert kw["transport_security"].enable_dns_rebinding_protection is True


def test_the_listener_is_stateless():
    # Every tool opens its own session and holds nothing between calls, so
    # there is no server-side state a session id would protect - and stateless
    # means a container reconnecting after a restart cannot land on an
    # expired session.
    from remem.mcp_server import http_run_kwargs

    assert http_run_kwargs("127.0.0.1", 9100)["stateless_http"] is True


def test_serving_http_pins_the_project_before_it_blocks(monkeypatch):
    from remem import mcp_server

    seen = {}

    def fake_run(transport, **kwargs):
        seen["transport"] = transport
        seen["kwargs"] = kwargs
        seen["project"] = mcp_server._default_project()

    monkeypatch.setattr(mcp_server.mcp, "run", fake_run)
    mcp_server.serve_http("192.168.64.3", 9100, "saddle")

    assert seen["transport"] == "streamable-http"
    assert seen["project"] == "saddle"
    assert seen["kwargs"]["host"] == "192.168.64.3"


def test_main_still_serves_stdio(monkeypatch):
    from remem import mcp_server

    seen = {}
    monkeypatch.setattr(
        mcp_server.mcp, "run", lambda *a, **k: seen.update(args=a, kwargs=k)
    )
    mcp_server.main()

    assert seen["args"] == ()
    assert seen["kwargs"] == {}
    assert mcp_server._http_mode is False


def _invoke(monkeypatch, argv):
    from typer.testing import CliRunner

    from remem.cli import app

    return CliRunner().invoke(app, argv)


def test_bare_serve_runs_stdio(monkeypatch):
    from remem import mcp_server

    called: dict[str, Any] = {}
    monkeypatch.setattr(mcp_server, "main", lambda: called.setdefault("stdio", True))
    monkeypatch.setattr(
        mcp_server, "serve_http", lambda *a: called.setdefault("http", a)
    )

    result = _invoke(monkeypatch, ["serve"])
    assert result.exit_code == 0
    assert called == {"stdio": True}


def test_serve_http_passes_host_port_and_project(monkeypatch):
    from remem import mcp_server

    called: dict[str, Any] = {}
    monkeypatch.setattr(mcp_server, "main", lambda: called.setdefault("stdio", True))
    monkeypatch.setattr(
        mcp_server, "serve_http", lambda *a: called.setdefault("http", a)
    )

    result = _invoke(
        monkeypatch,
        [
            "serve",
            "--http",
            "--host",
            "192.168.64.3",
            "--port",
            "9100",
            "--project",
            "saddle",
        ],
    )
    assert result.exit_code == 0
    assert called == {"http": ("192.168.64.3", 9100, "saddle")}


def test_serve_http_defaults_to_loopback_and_9100(monkeypatch):
    from remem import mcp_server

    called: dict[str, Any] = {}
    monkeypatch.setattr(
        mcp_server, "serve_http", lambda *a: called.setdefault("http", a)
    )

    result = _invoke(monkeypatch, ["serve", "--http"])
    assert result.exit_code == 0
    assert called == {"http": ("127.0.0.1", 9100, None)}
