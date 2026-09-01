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

# Re-measured 2026-08-31 against the real CLI on one 112-event session, five
# runs per row, counting runs that returned at least one entry:
#   40KB, tail-dropped to 18 events    -> 0 entries, 3 runs of 3
#   80KB, tail-dropped to 32 events    -> 0 entries, 3 runs of 3
#   83KB, all 112 events, values capped -> entries in 5 runs of 5
#   160KB, tail-dropped to 65 events   -> 2 / 1 / 4 entries
#   306KB, all 112 events, uncapped    -> 2 / 0 / 0 entries, 41s, exit 0
# COVERAGE is the variable, not size: 80KB of tail returned nothing three
# times while 83KB of whole session returned something five times. That is
# why the renderer caps values instead of dropping events, and why this
# budget has to be large enough to hold a capped working session - at 40,000
# the cap buys back half the coverage and the tail-drop takes it away again.
#
# The ceiling is real but further out than the previous note claimed. That
# note was measured on transcripts and predicted prose at 400KB; 306KB of
# event rows came back as clean JSON in 41 seconds. What degradation looks
# like here is a confident empty array, which `parse_entries` correctly
# reads as "found nothing" - indistinguishable from a quiet session. So the
# budget sits below where that was seen, not at it.
MAX_PROMPT_BYTES = 120_000

TRUNCATION_NOTE = (
    "[This is the TAIL of a longer session; earlier events were truncated.]\n"
)

EVENT_CUT_NOTE = " ...[this event's payload was cut short]"

# Below three events, hoisting a shared key saves exactly one copy of it -
# churn, and it takes a payload the reader could otherwise see whole. The
# saving is (n-1) copies, so three is where it starts being worth the
# indirection.
MIN_EVENTS_TO_HOIST = 3

CONSTANTS_NOTE = "[Constant for every event below, stated once: {}]\n"

# Cap one VALUE, rather than dropping a whole event, when the session does
# not fit. Measured on a 112-event session: dropping whole lines showed the
# model 18 events and returned nothing in three runs, while capping every
# value and showing all 112 returned entries in five runs out of five - at a
# quarter of the bytes. Coverage is what the model needs; a cut
# tool_response still says what the tool did, a dropped event says nothing.
#
# 400 over 200 and 800: yield across five runs each was 2.6 / 2.2 / 1.6
# entries at 200 / 400 / 800, but 200 produced almost all `note`, narrating
# the session, where 400 held the best share of durable `rule` entries. The
# ladder falls to 200 only when 400 will not fit, preferring a diluted
# whole session to a sharp fragment of one.
MAX_FIELD_BYTES = 400
TIGHT_FIELD_BYTES = 200

FIELD_CUT_NOTE = "...[cut]"


def _dump(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), default=str, sort_keys=True)


def constant_payload_keys(events: list[Event]) -> dict[str, object]:
    """Payload keys carrying one identical value across the whole batch.

    Derived from the batch rather than read off a table of key names,
    because this renderer serves every harness: Claude Code repeats `cwd`
    and `transcript_path` on every event, Cursor repeats `workspace_roots`
    and `user_email`, and a hardcoded list would be a fact about one
    harness sitting in a module that renders all three - the same mistake
    `agents/claude_code/env_vars.py` exists to avoid. Deriving also picks
    up whatever a future harness repeats, with nothing to maintain.

    A key must appear on EVERY event, not merely agree wherever it appears.
    Hoisting a key that four of five events carry would assert it of the
    fifth, which never said it.
    """
    if len(events) < MIN_EVENTS_TO_HOIST:
        return {}
    first = events[0].payload or {}
    shared: dict[str, object] = {}
    for key, value in first.items():
        rendered = _dump(value)
        if all(
            key in (e.payload or {})
            and _dump((e.payload or {})[key]) == rendered
            for e in events[1:]
        ):
            shared[key] = value
    return shared


def _cap(value: object, cap: int) -> object:
    """One payload value, trimmed to `cap` bytes of JSON if it exceeds it.

    Keyed on the value's size, never on its key's name. `tool_response` and
    `tool_input` are what overflow on Claude Code, but naming them here
    would be the harness-specific table that `constant_payload_keys` exists
    to avoid, and it would miss whatever the next harness calls the same
    thing.
    """
    text = _dump(value)
    if len(text) <= cap:
        return value
    return text[:cap] + FIELD_CUT_NOTE


def _render_event(
    event: Event, omit: set[str] | None = None, cap: int = MAX_FIELD_BYTES
) -> str:
    """One event, one line: when, what, which tool, and the raw payload.

    `omit` drops keys already stated once in the constants note, so the
    line carries only what distinguishes this event from its neighbours.
    """
    when = event.occurred_at.isoformat() if event.occurred_at else "unknown-time"
    tool = f" {event.tool}" if event.tool else ""
    body = {k: _cap(v, cap) for k, v in (event.payload or {}).items()
            if not omit or k not in omit}
    # separators= keeps the payload on one line and as small as possible: the
    # budget below is bytes, and every space spent on formatting is a line of
    # session the model does not get to see.
    return f"{when} {event.kind}{tool} {_dump(body)}"


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
    shared = constant_payload_keys(events)
    omit = set(shared)

    # Coverage first, per-event detail second, dropping events last. Each
    # rung shows the model every event; only when even the tight cap
    # overflows does the batch lose events off the front.
    lines: list[str] = []
    preamble = ""
    for cap in (MAX_FIELD_BYTES, TIGHT_FIELD_BYTES):
        stated = {k: _cap(v, cap) for k, v in shared.items()}
        preamble = CONSTANTS_NOTE.format(_dump(stated)) if stated else ""
        lines = [_render_event(e, omit=omit, cap=cap) for e in events]
        rendered = "\n".join(lines)
        if len(preamble) + len(rendered) <= limit:
            return preamble + rendered

    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        size += len(line) + 1
        if size > limit and kept:
            break
        kept.append(line[:limit] + EVENT_CUT_NOTE if len(line) > limit else line)
    kept.reverse()
    return preamble + TRUNCATION_NOTE + "\n".join(kept)


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
