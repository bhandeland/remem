"""The subprocess is asserted, never executed. No test spawns claude."""

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from remem.domain import Event, EventKind, new_id
from remem.extract.base import ExtractionFailed
from remem.extract.claude_cli import (
    MAX_PROMPT_BYTES,
    PROMPT,
    TRUNCATION_NOTE,
    ClaudeCliExtractor,
    build_command,
    build_env,
    render_events,
)

START = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def an_event(payload=None, i=0, tool="Bash", kind=EventKind.TOOL_CALL):
    return Event(
        id=new_id(), owner_id=new_id(), project="remem", harness="claude-code",
        session_id="s1", kind=kind, tool=tool,
        payload=payload if payload is not None else {"command": "ls"},
        occurred_at=START + timedelta(seconds=i),
    )


def events_of_size(total, count=40):
    """`count` events whose rendered form is at least `total` bytes."""
    filler = "x" * max(1, total // count)
    return [an_event({"command": filler}, i=i) for i in range(count)]


def test_command_runs_claude_in_print_mode():
    cmd = build_command("do the thing")
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "do the thing" in cmd


def test_env_sets_the_recursion_guard():
    """claude -p is itself a Claude Code session; without this its own hooks
    record the extraction's events, which the next extraction reads, and the
    whole thing compounds forever."""
    assert build_env({"PATH": "/usr/bin"})["REMEM_EXTRACT_CHILD"] == "1"


def test_env_preserves_the_caller_environment():
    assert build_env({"PATH": "/usr/bin", "HOME": "/h"})["PATH"] == "/usr/bin"


def test_prompt_forbids_recording_secrets():
    lowered = PROMPT.lower()
    assert "credential" in lowered or "secret" in lowered
    assert "token" in lowered


def test_prompt_permits_returning_nothing():
    assert "[]" in PROMPT


def test_extract_parses_the_subprocess_output(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout='[{"title":"T","body":"B","kind":"note"}]',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    entries = ClaudeCliExtractor().extract([an_event()], "remem")
    assert [e.title for e in entries] == ["T"]


def test_extract_raises_when_claude_is_missing(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ExtractionFailed) as exc:
        ClaudeCliExtractor().extract([an_event()], "remem")
    assert "not on PATH" in str(exc.value)


def test_extract_raises_on_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 180)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ExtractionFailed) as exc:
        ClaudeCliExtractor().extract([an_event()], "remem")
    assert "timed out" in str(exc.value)


def test_extract_raises_on_nonzero_exit(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ExtractionFailed) as exc:
        ClaudeCliExtractor().extract([an_event()], "remem")
    assert "boom" in str(exc.value)


def test_the_events_are_passed_on_stdin_not_as_an_argument(monkeypatch):
    """A megabyte of rendered events in argv would exceed the platform's
    argument limit."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["input"] = kwargs.get("input")
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ClaudeCliExtractor().extract([an_event({"command": "THE-EVENT"})], "remem")
    assert "THE-EVENT" in seen["input"]
    assert not any("THE-EVENT" in part for part in seen["cmd"])


# --- bounding the rendered events ------------------------------------------


def test_events_render_one_line_each_oldest_first():
    text = render_events([an_event({"command": "first"}, i=0),
                          an_event({"command": "second"}, i=1)])
    lines = text.splitlines()
    assert len(lines) == 2
    assert "first" in lines[0]
    assert "second" in lines[1]


def test_a_rendered_event_carries_its_time_kind_tool_and_payload():
    line = render_events([an_event({"command": "ls -l"})])
    assert START.isoformat() in line
    assert "tool_call" in line
    assert "Bash" in line
    assert '"command":"ls -l"' in line


def test_a_payload_is_rendered_whole_rather_than_summarised():
    """The judgement about what matters in a payload is the model's, and a
    renderer that pre-digested it would hide what a better prompt could
    find in the same stored events."""
    line = render_events([an_event({"a": 1, "nested": {"b": [1, 2]}})])
    assert '"nested":{"b":[1,2]}' in line


def test_an_event_with_no_tool_still_renders():
    line = render_events([an_event({"text": "hi"}, tool=None,
                                   kind=EventKind.MESSAGE)])
    assert "message" in line
    assert '"text":"hi"' in line


def test_a_large_batch_of_events_is_bounded_to_its_tail(monkeypatch):
    """Measured against the real CLI: 40KB follows the prompt and returns in
    seconds; 400KB takes ~5 minutes AND the model ignores the prompt
    entirely. An unbounded input fails on essentially every real session."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ClaudeCliExtractor().extract(events_of_size(MAX_PROMPT_BYTES * 3), "remem")
    # Tight, deliberately. The comment above MAX_PROMPT_BYTES calls oversized
    # input "the dangerous one: it does not announce itself" - this is the
    # test that announces it, so it must not have the slack to sleep through
    # a doubling. What stdin can legitimately hold is the rendered tail
    # (bounded at MAX_PROMPT_BYTES), the truncation note in front of it, and
    # the small header extract() adds. The prompt itself travels in argv.
    header = "Project: remem\n\nEvents:\n"
    ceiling = MAX_PROMPT_BYTES + len(TRUNCATION_NOTE) + len(header)
    assert len(seen["input"]) <= ceiling


def test_bounding_keeps_the_end_not_the_beginning():
    """A session's conclusions live at the end; its opening is setup."""
    events = (
        [an_event({"command": "START-MARKER"}, i=0)]
        + events_of_size(MAX_PROMPT_BYTES * 2)
        + [an_event({"command": "END-MARKER"}, i=9999)]
    )
    text = render_events(events)
    assert "END-MARKER" in text
    assert "START-MARKER" not in text


def test_a_bounded_batch_says_so():
    """The model must know it is seeing the end of a longer session, not a
    whole short one - otherwise it reasons about a truncated opening."""
    text = render_events(events_of_size(MAX_PROMPT_BYTES * 2))
    assert "truncated" in text.lower()


def test_truncation_never_cuts_a_line_in_half():
    """A half-rendered payload is a shape the model has to guess at."""
    events = events_of_size(MAX_PROMPT_BYTES * 2)
    body = render_events(events).split("\n", 1)[1]
    rendered = {render_events([e]) for e in events}
    assert all(line in rendered for line in body.splitlines())


def test_a_short_session_is_passed_whole():
    text = render_events([an_event({"command": "SHORT"})])
    assert "SHORT" in text
    assert "truncated" not in text.lower()


def test_no_events_renders_to_nothing():
    """The service treats an empty batch as a quiet session; the renderer
    must not invent a truncation note for it."""
    assert render_events([]) == ""


# --- diagnosable failures ---------------------------------------------------


def test_a_nonzero_exit_with_empty_stderr_still_says_something_useful(monkeypatch):
    """The observed real-world failure: `claude exited 1:` and nothing more.
    Exit code alone is not a diagnosis."""
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ExtractionFailed) as exc:
        ClaudeCliExtractor().extract([an_event()], "remem")
    message = str(exc.value)
    assert "exited 1" in message
    assert "no stderr" in message.lower()
    assert "bytes" in message.lower()


def test_a_nonzero_exit_with_stderr_still_includes_it(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom happened")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ExtractionFailed) as exc:
        ClaudeCliExtractor().extract([an_event()], "remem")
    assert "boom happened" in str(exc.value)


def test_a_timeout_reports_the_input_size(monkeypatch):
    """Size is the first thing to suspect on a timeout."""
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 180)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ExtractionFailed) as exc:
        ClaudeCliExtractor().extract(events_of_size(5000), "remem")
    assert "bytes" in str(exc.value).lower()


# --- model pinning ----------------------------------------------------------


def test_the_command_pins_a_model():
    """Without --model, `claude -p` inherits the user's session model, so
    extraction silently changes whenever they switch models for unrelated
    reasons."""
    cmd = build_command("prompt text", model="sonnet")
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "sonnet"


def test_the_extractor_passes_its_configured_model(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ClaudeCliExtractor(model="haiku").extract([an_event()], "remem")
    assert seen["cmd"][seen["cmd"].index("--model") + 1] == "haiku"


def test_the_extractor_defaults_to_the_configured_default(monkeypatch):
    from remem.config import DEFAULT_EXTRACT_MODEL

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ClaudeCliExtractor().extract([an_event()], "remem")
    assert seen["cmd"][seen["cmd"].index("--model") + 1] == DEFAULT_EXTRACT_MODEL


def test_one_event_larger_than_the_whole_budget_is_cut(monkeypatch):
    """A single Read of a large file would otherwise defeat the bound: the
    line-at-a-time rule keeps whole lines, and one line can be the session."""
    text = render_events([an_event({"content": "y" * (MAX_PROMPT_BYTES * 2)})])
    assert len(text) < MAX_PROMPT_BYTES * 2
    assert "cut short" in text
