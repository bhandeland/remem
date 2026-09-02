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


def read_document(path: Path) -> dict:
    """Read a JSON object, tolerating everything, writing nothing.

    Deliberately NOT `read_json`: that helper backs a corrupt file up before
    returning an empty document, which is right for an install about to
    rewrite the file and wrong for a check. A diagnostic that leaves .bak
    files behind beside a user's config is a diagnostic people stop running,
    and a diagnostic nobody runs is the state this whole area of the code is
    trying to get out of.

    A missing file, an unreadable one, invalid JSON and a top-level value
    that is not an object all read as `{}` - the honest answer for a caller
    asking what a config registers, since a file it cannot parse names
    nothing that can be found. Callers who need to know the difference
    should stat the path themselves.

    Lives here rather than on an adapter because it is not harness-specific:
    the Claude Code and Cursor `hook_state()` implementations both need it,
    and the copy that carried this reasoning was on only one of them - which
    is exactly how the third adapter would end up copying the version
    without it, or routing the check through `read_json` for tidiness.
    """
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_json(path: Path, data: dict, backed_up: set[Path]) -> Path | None:
    """Write data, backing the existing file up first. Returns the backup."""
    made = backup_once(path, backed_up)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return made
