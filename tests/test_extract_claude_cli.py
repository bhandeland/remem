"""The subprocess is asserted, never executed. No test spawns claude."""

import json
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
        id=new_id(),
        owner_id=new_id(),
        project="remem",
        harness="claude-code",
        session_id="s1",
        kind=kind,
        tool=tool,
        payload=payload if payload is not None else {"command": "ls"},
        occurred_at=START + timedelta(seconds=i),
    )


def events_of_size(total, count=1000):
    """`count` events whose rendered form is at least `total` bytes.

    Each payload differs: a real session does not repeat one command
    hundreds of times, and identical payloads would be hoisted into the
    constants note and leave these fixtures with nothing left to bound.

    Many modest events rather than a few enormous ones, because that is now
    the only shape that can overflow the budget: per-value capping trims the
    outliers, so what remains is the sheer number of events. `count` has to
    be high enough that the batch overflows even at the TIGHT cap - a batch
    that merely overflows at the default cap is re-rendered tighter and
    fits, which is the ladder working, not bounding.
    """
    filler = "x" * max(1, total // count)
    return [an_event({"command": f"{i}-{filler}"}, i=i) for i in range(count)]


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
            cmd,
            0,
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
    text = render_events(
        [an_event({"command": "first"}, i=0), an_event({"command": "second"}, i=1)]
    )
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
    line = render_events([an_event({"text": "hi"}, tool=None, kind=EventKind.MESSAGE)])
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
    """A half-rendered payload is a shape the model has to guess at, and the
    first thing it guesses is that the payload means something other than
    what it says. Asserted on the JSON itself rather than by comparing with
    a single-event render: the batch may have been rendered at a tighter
    field cap than one event alone would be, which is a whole line all the
    same."""
    text = render_events(events_of_size(MAX_PROMPT_BYTES * 2))
    body = [ln for ln in text.splitlines() if not ln.startswith("[")]
    assert len(body) > 1
    for line in body:
        assert json.loads(line.split(" ", 3)[3])


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
    """A single Read of a large file would otherwise defeat the bound. The
    field cap now catches this before the line-at-a-time rule does - the
    event survives, only its oversized value is trimmed."""
    text = render_events([an_event({"content": "y" * (MAX_PROMPT_BYTES * 2)})])
    assert len(text) < MAX_PROMPT_BYTES * 2
    assert "cut" in text.lower()
    assert "tool_call" in text


def test_a_line_that_is_still_too_long_after_capping_is_cut_short():
    """The backstop beneath the field cap: capping bounds each value, not
    their number, so an event carrying hundreds of them can still be longer
    than the whole budget on its own."""
    payload = {f"k{i}": "y" * 1000 for i in range(300)}
    text = render_events([an_event(payload)], limit=10_000)
    assert len(text) < 12_000
    assert "cut short" in text


# --- constants stated once, not per event -----------------------------------


def _lines(text):
    """The event lines, without the bracketed notes the renderer prepends."""
    return [ln for ln in text.splitlines() if not ln.startswith("[")]


def _notes(text):
    return [ln for ln in text.splitlines() if ln.startswith("[")]


def test_a_key_identical_across_every_event_is_stated_once():
    """112 events repeating one cwd spent ~4KB saying the same thing. The
    budget is bytes, and every repeat is a line of session the model does
    not get to see."""
    events = [an_event({"cwd": "/repo", "command": f"c{i}"}, i=i) for i in range(5)]
    text = render_events(events)
    assert "/repo" in "\n".join(_notes(text))
    assert all("/repo" not in line for line in _lines(text))
    assert all(f"c{i}" in text for i in range(5))


def test_a_key_whose_value_varies_stays_on_every_event():
    events = [an_event({"cwd": f"/repo{i}", "command": "x"}, i=i) for i in range(5)]
    text = render_events(events)
    assert all(f"/repo{i}" in "\n".join(_lines(text)) for i in range(5))


def test_a_key_missing_from_one_event_is_not_hoisted():
    """Present-and-equal on four of five events is not constant: hoisting it
    would assert it of the fifth, which never carried it."""
    events = [an_event({"cwd": "/repo"}, i=i) for i in range(4)]
    events.append(an_event({"command": "no-cwd"}, i=4))
    text = render_events(events)
    assert "/repo" in "\n".join(_lines(text))


def test_a_batch_too_short_to_repeat_itself_is_left_alone():
    """Below three events hoisting saves at most one copy - churn, and it
    would strip the payload the caller can see whole today."""
    events = [an_event({"cwd": "/repo"}, i=i) for i in range(2)]
    text = render_events(events)
    assert _notes(text) == []
    assert all("/repo" in line for line in _lines(text))


def test_hoisting_is_keyed_on_the_batch_not_on_a_table_of_key_names():
    """The renderer serves every harness. A hardcoded list of Claude Code's
    payload keys would silently do nothing for Cursor, whose constants are
    workspace_roots and user_email."""
    events = [
        an_event(
            {"workspace_roots": ["/w"], "generation_id": f"g{i}"},
            i=i,
            tool=None,
            kind=EventKind.MESSAGE,
        )
        for i in range(5)
    ]
    text = render_events(events)
    assert "/w" in "\n".join(_notes(text))
    assert all("/w" not in line for line in _lines(text))


# --- capping one value, rather than dropping the whole event ----------------


def test_a_value_larger_than_the_field_cap_is_cut_whatever_its_key():
    """Keyed on the value's size, not on a list of key names: one 31KB
    tool_response took 77% of the whole budget on a real session, and the
    key holding it differs per harness."""
    big = "z" * 5000
    events = [an_event({f"odd_key_{i}": big, "n": i}, i=i) for i in range(5)]
    text = render_events(events)
    assert big not in text
    assert "cut" in text.lower()


def test_a_value_within_the_cap_is_left_whole():
    """The cap trims the outliers; it is not a summariser."""
    modest = "y" * 300
    events = [an_event({"command": modest, "n": i}, i=i) for i in range(5)]
    assert modest in render_events(events)


def test_a_constant_too_large_to_state_is_cut_in_the_note_too():
    """Hoisting a 30KB constant would state it once and still blow the
    budget - stating it once is not the same as stating it cheaply."""
    big = "z" * 5000
    events = [an_event({"blob": big, "n": i}, i=i) for i in range(5)]
    text = render_events(events)
    assert big not in text
    assert "blob" in text


def test_capping_a_value_keeps_every_event(monkeypatch):
    """The point of the cap: a session stays whole. Dropping events was
    measured at zero entries on a session holding four durable insights,
    where the capped render of the same events returned entries in five
    runs out of five."""
    events = [
        an_event({"out": f"{i}-" + "z" * 4000, "marker": f"EVENT-{i}"}, i=i)
        for i in range(30)
    ]
    text = render_events(events, limit=30_000)
    assert all(f"EVENT-{i}" in text for i in range(30))
    assert "truncated" not in text.lower()


def test_a_whole_working_session_fits_within_the_budget():
    """The measured shape this budget exists to hold: ~112 events whose
    capped render is ~83KB. At the old 40,000 the same session was dropped
    to its last 18 events and returned nothing in three runs, where the
    whole session returned entries in five out of five."""
    events = [
        an_event(
            {
                "tool_response": f"E{i}-" + "z" * 3000,
                "tool_input": f"I{i}-" + "q" * 3000,
                "cwd": "/repo",
            },
            i=i,
        )
        for i in range(112)
    ]
    text = render_events(events)
    assert "truncated" not in text.lower()
    assert all(f"E{i}-" in text for i in range(112))


def test_a_session_too_large_at_the_default_cap_is_re_rendered_tighter():
    """The middle rung. Every event is still shown; each is told less. A
    diluted whole session beat a sharp fragment of one in every measured
    run, so detail is what gives way first, not coverage."""
    events = [an_event({"out": f"E{i}-" + "z" * 1000}, i=i) for i in range(100)]
    text = render_events(events, limit=30_000)
    assert all(f"E{i}-" in text for i in range(100))
    assert "truncated" not in text.lower()
