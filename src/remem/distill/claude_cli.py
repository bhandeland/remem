"""Distillation by shelling out to the Claude Code CLI.

Chosen over the Anthropic API because it reuses the user's existing Claude Code
authentication: no second API key, no second bill, nothing to configure before
capture works.
"""

from __future__ import annotations

import os
import subprocess
from typing import Mapping

from remem.distill.base import CHILD_ENV_VAR, CapturedEntry, DistillationFailed, parse_entries

PROMPT = """\
You are reading a transcript of a coding session to extract durable knowledge.

Return a JSON array of at most 5 objects, each shaped:
  {"title": "...", "body": "...", "kind": "memory|doc|rule", "tags": ["..."]}

Record only what will still be true and useful in a month:
- a decision whose reasoning is not obvious from the code
- a non-obvious gotcha that cost real time
- a convention or preference that should hold in future (kind: "rule")
- how a system actually behaves, where the repo does not say

Record NOTHING that contains credentials, API keys, tokens, secrets,
environment variable values, or file contents. Record nothing transient: what
was being worked on, what a specific test run printed, or where a task got to.
Do not restate what the code, README, or git history already says.

If the session contains nothing durable, return [] - that is the common and
correct answer, and is much better than inventing something.

Return ONLY the JSON array.
"""


def build_command(prompt: str) -> list[str]:
    return ["claude", "-p", prompt]


def build_env(base: Mapping[str, str]) -> dict[str, str]:
    """Environment for the child, carrying the recursion guard.

    claude -p starts a real Claude Code session, which fires its own SessionEnd
    hook. Without this flag that hook enqueues another job, which spawns another
    session, without bound.
    """
    env = dict(base)
    env[CHILD_ENV_VAR] = "1"
    return env


class ClaudeCliDistiller:
    def __init__(self, timeout: int = 180) -> None:
        self._timeout = timeout

    def distill(self, transcript: str, project: str) -> list[CapturedEntry]:
        try:
            result = subprocess.run(
                build_command(PROMPT),
                input=f"Project: {project}\n\nTranscript:\n{transcript}",
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=build_env(os.environ),
            )
        except FileNotFoundError as exc:
            raise DistillationFailed(
                "claude is not on PATH; capture needs the Claude Code CLI"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DistillationFailed(
                f"claude timed out after {self._timeout}s"
            ) from exc

        if result.returncode != 0:
            raise DistillationFailed(
                f"claude exited {result.returncode}: {result.stderr[:300]}"
            )
        return parse_entries(result.stdout)
