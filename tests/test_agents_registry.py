from importlib.metadata import EntryPoint, entry_points

import pytest

import remem.agents.registry as registry
from remem.agents.registry import UnknownAgent, discover, get


def test_claude_code_is_discovered():
    assert "claude-code" in discover()


def test_get_returns_the_adapter_class():
    adapter = get("claude-code")
    assert adapter.name == "claude-code"


def test_unknown_agent_raises_with_a_helpful_message():
    with pytest.raises(UnknownAgent) as exc:
        get("emacs-doctor")
    assert "claude-code" in str(exc.value)


def test_a_broken_entry_point_is_reported_but_does_not_break_discovery(monkeypatch):
    real = list(entry_points(group=registry.GROUP))
    broken = EntryPoint(
        name="broken-agent",
        value="remem.agents.nonexistent_module:NoSuchAdapter",
        group=registry.GROUP,
    )

    def fake_entry_points(*, group):
        assert group == registry.GROUP
        return real + [broken]

    monkeypatch.setattr(registry, "entry_points", fake_entry_points)

    with pytest.warns(UserWarning, match="broken-agent"):
        found = registry.discover()

    assert "claude-code" in found
    assert "broken-agent" not in found
