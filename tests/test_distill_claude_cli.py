"""The subprocess is asserted, never executed. No test spawns claude."""

import subprocess

import pytest

from remem.distill.base import DistillationFailed
from remem.distill.claude_cli import (
    PROMPT,
    ClaudeCliDistiller,
    build_command,
    build_env,
)


def test_command_runs_claude_in_print_mode():
    cmd = build_command("do the thing")
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "do the thing" in cmd


def test_env_sets_the_recursion_guard():
    """claude -p is itself a Claude Code session; without this its SessionEnd
    hook enqueues another job and the whole thing compounds forever."""
    assert build_env({"PATH": "/usr/bin"})["REMEM_CAPTURE_CHILD"] == "1"


def test_env_preserves_the_caller_environment():
    assert build_env({"PATH": "/usr/bin", "HOME": "/h"})["PATH"] == "/usr/bin"


def test_prompt_forbids_recording_secrets():
    lowered = PROMPT.lower()
    assert "credential" in lowered or "secret" in lowered
    assert "token" in lowered


def test_prompt_permits_returning_nothing():
    assert "[]" in PROMPT


def test_distill_parses_the_subprocess_output(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout='[{"title":"T","body":"B","kind":"memory"}]',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    entries = ClaudeCliDistiller().distill("transcript text", "remem")
    assert [e.title for e in entries] == ["T"]


def test_distill_raises_when_claude_is_missing(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DistillationFailed) as exc:
        ClaudeCliDistiller().distill("t", "remem")
    assert "not on PATH" in str(exc.value)


def test_distill_raises_on_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 180)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DistillationFailed) as exc:
        ClaudeCliDistiller().distill("t", "remem")
    assert "timed out" in str(exc.value)


def test_distill_raises_on_nonzero_exit(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DistillationFailed) as exc:
        ClaudeCliDistiller().distill("t", "remem")
    assert "boom" in str(exc.value)


def test_transcript_is_passed_on_stdin_not_as_an_argument(monkeypatch):
    """A megabyte transcript in argv would exceed the platform's arg limit."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["input"] = kwargs.get("input")
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ClaudeCliDistiller().distill("THE-TRANSCRIPT", "remem")
    assert "THE-TRANSCRIPT" in seen["input"]
    assert not any("THE-TRANSCRIPT" in part for part in seen["cmd"])


# --- transcript bounding ----------------------------------------------------


def test_a_large_transcript_is_bounded_to_its_tail(monkeypatch):
    """Measured against the real CLI: a 40KB transcript follows the prompt and
    returns in seconds; a 400KB one takes ~5 minutes AND the model ignores the
    prompt entirely, continuing the transcript's conversation instead. An
    unbounded transcript makes capture fail on essentially every real session.
    """
    from remem.distill.claude_cli import MAX_TRANSCRIPT_BYTES

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    huge = "x" * (MAX_TRANSCRIPT_BYTES * 3)
    ClaudeCliDistiller().distill(huge, "remem")
    assert len(seen["input"]) < MAX_TRANSCRIPT_BYTES * 2


def test_bounding_keeps_the_end_not_the_beginning(monkeypatch):
    """A session's conclusions live at the end; its opening is throat-clearing."""
    from remem.distill.claude_cli import MAX_TRANSCRIPT_BYTES

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    transcript = "START-MARKER" + ("x" * MAX_TRANSCRIPT_BYTES * 2) + "END-MARKER"
    ClaudeCliDistiller().distill(transcript, "remem")
    assert "END-MARKER" in seen["input"]
    assert "START-MARKER" not in seen["input"]


def test_a_bounded_transcript_says_so(monkeypatch):
    """The model must know it is seeing the end of a longer session, not a
    whole short one - otherwise it reasons about a truncated opening."""
    from remem.distill.claude_cli import MAX_TRANSCRIPT_BYTES

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ClaudeCliDistiller().distill("x" * (MAX_TRANSCRIPT_BYTES * 2), "remem")
    assert "truncated" in seen["input"].lower()


def test_a_small_transcript_is_passed_whole(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ClaudeCliDistiller().distill("SHORT-TRANSCRIPT", "remem")
    assert "SHORT-TRANSCRIPT" in seen["input"]
    assert "truncated" not in seen["input"].lower()


# --- diagnosable failures ---------------------------------------------------


def test_a_nonzero_exit_with_empty_stderr_still_says_something_useful(monkeypatch):
    """The observed real-world failure: `claude exited 1:` and nothing more.
    Exit code alone is not a diagnosis."""
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DistillationFailed) as exc:
        ClaudeCliDistiller().distill("some transcript", "remem")
    message = str(exc.value)
    assert "exited 1" in message
    assert "no stderr" in message.lower()
    assert "bytes" in message.lower()


def test_a_nonzero_exit_with_stderr_still_includes_it(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom happened")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DistillationFailed) as exc:
        ClaudeCliDistiller().distill("t", "remem")
    assert "boom happened" in str(exc.value)


def test_a_timeout_reports_the_input_size(monkeypatch):
    """Size is the first thing to suspect on a timeout."""
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 180)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DistillationFailed) as exc:
        ClaudeCliDistiller().distill("t" * 5000, "remem")
    assert "bytes" in str(exc.value).lower()
