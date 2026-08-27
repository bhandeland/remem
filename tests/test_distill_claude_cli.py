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
