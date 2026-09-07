"""How long a session has run, and when to say so.

Runs on every user prompt, so it does bounded work and never touches Postgres.
Deliberately not under agents/claude_code/: counting turns in a transcript is
not agent-specific policy. The hook that calls this is.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from platformdirs import user_cache_path

STATE_TTL_SECONDS = 7 * 24 * 60 * 60


def count_turns(path: str | Path) -> int:
    """User turns in a transcript, or 0 if it cannot be read.

    Matched per line rather than parsed as JSON: the transcript reaches
    megabytes in exactly the sessions this exists for, and the count only has
    to be right enough to decide when to warn.
    """
    total = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"type":"user"' in line or '"type": "user"' in line:
                    total += 1
    except OSError:
        return 0
    return total


def should_warn(count: int, last_warned: int, warn_at: int, warn_every: int) -> bool:
    """True on the first prompt at or past each step.

    Compared against the last warned count rather than tested with a modulo:
    the count does not always advance by exactly one per prompt, and a skipped
    remainder would mean the warning never fires again for that session.
    """
    if count < warn_at:
        return False
    if last_warned <= 0:
        return True
    return count - last_warned >= warn_every


def state_path() -> Path:
    return user_cache_path("remem") / "session-size.json"


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except OSError, ValueError:
        # Unreadable or corrupt reads as "never warned". Both directions of
        # this file fail toward warning rather than toward silence.
        return {}
    return data if isinstance(data, dict) else {}


def read_last_warned(session_id: str, path: Path | None = None) -> int:
    sessions = _load(path or state_path()).get("sessions") or {}
    record = sessions.get(session_id) or {}
    try:
        return int(record.get("count", 0))
    except TypeError, ValueError:
        return 0


def record_warned(
    session_id: str,
    count: int,
    path: Path | None = None,
    now: float | None = None,
) -> None:
    """Best effort. A failed write means the next prompt warns again."""
    path = path or state_path()
    now = time.time() if now is None else now
    data = _load(path)
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    sessions[session_id] = {"count": int(count), "at": now}
    # Pruned on write rather than on a schedule: nothing else ever opens this
    # file, so this is the only moment it can be trimmed.
    sessions = {
        sid: rec
        for sid, rec in sessions.items()
        if isinstance(rec, dict) and now - float(rec.get("at", 0)) <= STATE_TTL_SECONDS
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sessions": sessions}))
    except OSError:
        pass


def reminder(count: int) -> str:
    return (
        f"This session is {count} turns long. At the next task boundary - not "
        f"mid-task - offer the user a handoff: run the remem-handoff skill, "
        f"then /clear, then remem-prime to resume. Do not interrupt work in "
        f"progress to do it."
    )
