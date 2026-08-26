import pytest

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
