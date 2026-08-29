"""Extraction by shelling out to the Claude Code CLI.

Chosen over the Anthropic API because it reuses the user's existing Claude Code
authentication: no second API key, no second bill, nothing to configure before
extraction works.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Mapping

from remem.config import DEFAULT_EXTRACT_MODEL
from remem.domain import Event
from remem.extract.base import (
    CHILD_ENV_VAR,
    ExtractedEntry,
    ExtractionFailed,
    parse_entries,
)

# Measured against the real CLI on a long session's transcript:
#   40KB  -> returns in seconds, and the model follows the prompt
#   400KB -> ~5 minutes (past any sane timeout), AND the model ignores the
#            prompt entirely, continuing the conversation instead of
#            extracting from it
#   5.9MB -> claude exits 1
# The second case is the dangerous one: it does not announce itself. Output
# comes back as prose, parse_entries raises, and the job looks like a bad
# prompt rather than an oversized input. The bound applies to the RENDERED
# events for the same reason it applied to the transcript - it is a property
# of the model's attention, not of where the text came from.
MAX_PROMPT_BYTES = 40_000

TRUNCATION_NOTE = (
    "[This is the TAIL of a longer session; earlier events were truncated.]\n"
)

EVENT_CUT_NOTE = " ...[this event's payload was cut short]"


def _render_event(event: Event) -> str:
    """One event, one line: when, what, which tool, and the raw payload."""
    when = event.occurred_at.isoformat() if event.occurred_at else "unknown-time"
    tool = f" {event.tool}" if event.tool else ""
    # separators= keeps the payload on one line and as small as possible: the
    # budget below is bytes, and every space spent on formatting is a line of
    # session the model does not get to see.
    payload = json.dumps(event.payload or {}, separators=(",", ":"),
                         default=str, sort_keys=True)
    return f"{when} {event.kind}{tool} {payload}"


def render_events(events: list[Event], limit: int = MAX_PROMPT_BYTES) -> str:
    """One line per event, oldest first, newest kept.

    Keeping the END for the same reason the transcript version did: a
    session's conclusions, decisions and corrections live at its end, and its
    opening is setup. The note matters as much as the truncation - without
    it the model reasons about a session that appears to begin mid-thought.

    The payload is rendered as compact JSON rather than summarised. What is
    worth keeping from a tool call is exactly the judgement this pipeline
    delegates to the model, and pre-digesting it here would make the
    extractor unable to see anything the renderer decided to drop - while
    the whole point of storing raw is that a better prompt can be re-run
    over the same events.

    Truncation is by whole lines from the front, not by character: cutting
    mid-JSON would hand the model a fragment it has to guess the shape of,
    and the first thing it would guess is that the payload means something
    other than what it says. The exception is a single event bigger than the
    whole budget - one `Read` of a large file will do it - where keeping the
    line whole would defeat the bound entirely. That one gets cut, and told
    that it was.
    """
    lines = [_render_event(e) for e in events]
    rendered = "\n".join(lines)
    if len(rendered) <= limit:
        return rendered

    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        size += len(line) + 1
        if size > limit and kept:
            break
        kept.append(line[:limit] + EVENT_CUT_NOTE if len(line) > limit else line)
    kept.reverse()
    return TRUNCATION_NOTE + "\n".join(kept)


PROMPT = """\
You are reading a list of events recorded from one coding session, oldest
first, to extract durable knowledge. Each line is one event: its timestamp,
its kind, the tool it used if any, and its raw JSON payload.

Return a JSON array of at most 5 objects, each shaped:
  {"title": "...", "body": "...", "kind": "note|doc|rule", "tags": ["..."]}

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


MAX_KNOWN_TITLES = 60


def build_prompt(known_titles: list[str] | None = None) -> str:
    """The extraction prompt, told what this project already holds.

    Without this the extractor has no idea anything is recorded, so it happily
    re-derives a rule the user wrote by hand an hour earlier. Dedup cannot
    catch that: the wording differs, so an exact-title match sees two distinct
    entries. Prevention has to happen before the model writes, not after.

    Titles only, and capped: a project's whole store would crowd out the
    session it is supposed to be reading.
    """
    titles = [t.strip() for t in (known_titles or []) if t and t.strip()]
    if not titles:
        return PROMPT
    listed = "\n".join(f"- {t}" for t in titles[:MAX_KNOWN_TITLES])
    return (
        f"{PROMPT}\n"
        "Already recorded for this project - do NOT record any of these "
        "again, even reworded:\n"
        f"{listed}\n"
    )


def build_command(prompt: str, model: str = DEFAULT_EXTRACT_MODEL) -> list[str]:
    """Always pin the model.

    Without --model, `claude -p` inherits the user's session model, so
    extraction would silently get more expensive the moment they switch to a
    stronger model for unrelated reasons.
    """
    return ["claude", "-p", "--model", model, prompt]


def build_env(base: Mapping[str, str]) -> dict[str, str]:
    """Environment for the child, carrying the recursion guard.

    claude -p starts a real Claude Code session, which fires its own hooks.
    Without this flag those hooks record the extraction's own events, which
    the next extraction then reads and extracts, without bound.
    """
    env = dict(base)
    env[CHILD_ENV_VAR] = "1"
    return env


class ClaudeCliExtractor:
    def __init__(
        self, timeout: int = 180, model: str = DEFAULT_EXTRACT_MODEL
    ) -> None:
        self._timeout = timeout
        self._model = model

    def extract(
        self,
        events: list[Event],
        project: str,
        known_titles: list[str] | None = None,
    ) -> list[ExtractedEntry]:
        payload = (
            f"Project: {project}\n\nEvents:\n{render_events(events)}"
        )
        size = len(payload)
        try:
            result = subprocess.run(
                build_command(build_prompt(known_titles), self._model),
                input=payload,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=build_env(os.environ),
            )
        except FileNotFoundError as exc:
            raise ExtractionFailed(
                "claude is not on PATH; extraction needs the Claude Code CLI"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ExtractionFailed(
                f"claude timed out after {self._timeout}s on {size} bytes of input"
            ) from exc

        if result.returncode != 0:
            # Observed in the wild: exit 1 with completely empty stderr. The
            # exit code alone is not a diagnosis, so say what was sent too.
            detail = result.stderr[:300].strip() or "no stderr output"
            raise ExtractionFailed(
                f"claude exited {result.returncode} on {size} bytes of input: "
                f"{detail}"
            )
        return parse_entries(result.stdout)
