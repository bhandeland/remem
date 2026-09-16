"""Claude Code's real transcript layout, built for tests.

The first transcript fixtures only ever wrote a flat directory of
`<session-id>.jsonl` files - the same assumption the code made, which is
why no test noticed the import never read a subagent's transcript. A
fixture that mirrors only what the author already believes cannot
falsify the belief. So the nested shape lives here, once, and every
transcript test builds from it.

The layout is spelled LITERALLY - "subagents", "agent-" - and never taken
from the service's constants: this is Claude Code's contract, and a
fixture built from our own names would stay green through a rename that
broke the real thing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _dump(target: Path, lines: list[dict[str, Any]]) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"".join(json.dumps(line).encode() + b"\n" for line in lines))
    return target


def write_session(
    directory: Path, session_id: str, lines: list[dict[str, Any]] | None = None
) -> Path:
    """`<directory>/<session_id>.jsonl` - a session's own transcript."""
    if lines is None:
        lines = [{"type": "user", "sessionId": session_id}]
    return _dump(directory / f"{session_id}.jsonl", lines)


def write_subagent(
    directory: Path,
    session_id: str,
    agent_id: str,
    lines: list[dict[str, Any]] | None = None,
) -> Path:
    """`<directory>/<session_id>/subagents/agent-<agent_id>.jsonl`.

    The default lines carry what real ones do: the PARENT's `sessionId`,
    the file's own `agentId`, and `isSidechain: true`.
    """
    if lines is None:
        lines = [
            {
                "type": "user",
                "sessionId": session_id,
                "agentId": agent_id,
                "isSidechain": True,
            }
        ]
    return _dump(
        directory / session_id / "subagents" / f"agent-{agent_id}.jsonl", lines
    )


def write_tool_result(directory: Path, session_id: str) -> Path:
    """`<directory>/<session_id>/tool-results/hook-...-stdout.txt`.

    One of two other things Claude Code writes below a session directory.
    Not a transcript; nothing may import it.
    """
    target = directory / session_id / "tool-results" / "hook-0000-stdout.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hook output\n")
    return target


def write_subagent_meta(directory: Path, session_id: str, agent_id: str) -> Path:
    """`<directory>/<session_id>/subagents/agent-<agent_id>.meta.json`.

    Claude Code writes one beside every subagent transcript; it describes
    the subagent; it is not a transcript.
    """
    target = directory / session_id / "subagents" / f"agent-{agent_id}.meta.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "agentType": "implementer",
                "description": "Implement Task 1",
                "spawnDepth": 0,
            }
        )
    )
    return target
