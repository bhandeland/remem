"""Safe edit-in-place of a JSON config file remem does not own.

Agent-neutral on purpose: both the Claude Code adapter (writing hooks at
install time) and services/settings.py (writing the env block later) edit the
same settings.json, and a second copy of the backup logic is how the two
drift apart.

read_json returns warnings rather than appending to an InstallReport: the
settings service has no report to append to, and coupling a file helper to an
install-time dataclass is what kept this logic trapped in the adapter.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path


def backup(path: Path) -> Path:
    ts = int(time.time())
    target = path.with_suffix(path.suffix + f".bak{ts}")
    counter = 0
    while target.exists():
        counter += 1
        target = path.with_suffix(path.suffix + f".bak{ts}-{counter}")
    shutil.copy2(path, target)
    return target


def backup_once(path: Path, backed_up: set[Path]) -> Path | None:
    """Back up path if it exists on disk and hasn't already been backed up
    during this run (avoids a redundant second backup of a file read_json
    already snapshotted because it was corrupt).

    Returns where the copy went, or None if no copy was made - either the
    file did not exist yet or this run has already snapshotted it. The
    caller is what tells the user; a timestamped `.bakNNNNNNNNNN` sitting
    silently beside the file is a safety net nobody knows to look for.
    """
    if path.exists() and path not in backed_up:
        target = backup(path)
        backed_up.add(path)
        return target
    return None


def read_json(path: Path, backed_up: set[Path]) -> tuple[dict, list[str]]:
    if not path.exists():
        return {}, []
    raw = path.read_text()
    try:
        return json.loads(raw), []
    except json.JSONDecodeError:
        backup_once(path, backed_up)
        return {}, [
            f"{path} was not valid JSON. It has been backed up and replaced; "
            "check the backup for anything you need."
        ]


def write_json(path: Path, data: dict, backed_up: set[Path]) -> Path | None:
    """Write data, backing the existing file up first. Returns the backup."""
    made = backup_once(path, backed_up)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return made
