"""Telling the extractor what is already recorded.

Observed live: a rule written by hand was re-derived by capture twenty minutes
later in the same session, with a differently-worded title. Dedup could not
catch it - it matches exact titles, and only against prior extractions. The cause
is upstream of dedup: the extractor never saw the store, so re-recording
something already written is the expected outcome rather than a bug.
"""

import subprocess
from datetime import datetime, timezone

from remem.domain import Event, EventKind, new_id
from remem.extract.claude_cli import ClaudeCliExtractor, build_prompt


def an_event(payload=None):
    return Event(
        id=new_id(), owner_id=new_id(), project="remem", harness="claude-code",
        session_id="s1", kind=EventKind.TOOL_CALL, tool="Bash",
        payload=payload or {"command": "ls"},
        occurred_at=datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc),
    )


def test_the_prompt_lists_what_is_already_recorded():
    prompt = build_prompt(["Pin the model for any repeated LLM call"])
    assert "Pin the model for any repeated LLM call" in prompt


def test_the_prompt_says_not_to_repeat_them():
    prompt = build_prompt(["Some existing title"])
    lowered = prompt.lower()
    assert "already" in lowered
    assert "again" in lowered or "not record" in lowered


def test_the_prompt_is_unchanged_when_nothing_is_recorded_yet():
    """A fresh project should not carry an empty 'already recorded' section
    that reads as an instruction with no content."""
    assert "already recorded" not in build_prompt([]).lower()


def test_known_titles_are_bounded():
    """A project with hundreds of entries must not push the session's own
    events out of the context window with its titles."""
    from remem.extract.claude_cli import MAX_KNOWN_TITLES

    prompt = build_prompt([f"Title number {i}" for i in range(MAX_KNOWN_TITLES * 3)])
    assert prompt.count("Title number") <= MAX_KNOWN_TITLES


def test_the_extractor_passes_known_titles_into_the_prompt(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ClaudeCliExtractor().extract(
        [an_event()], "remem", known_titles=["An existing rule"]
    )
    assert any("An existing rule" in part for part in seen["cmd"])


def test_known_titles_are_optional(monkeypatch):
    """The protocol's older two-argument call must keep working."""
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ClaudeCliExtractor().extract([an_event()], "remem") == []
