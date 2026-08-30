"""Where Cursor's hooks.json lives, and how remem merges into it."""

from __future__ import annotations

import json
import time
from pathlib import Path

from remem.agents.base import UnsupportedScope

#: The hook entries remem installs, hook name to command.
#:
#: `remem record event --agent cursor`, NOT `remem hook record-event` -
#: that one is hardcoded to the Claude Code hook and takes no --agent, so
#: naming it here would record nothing, silently. A test pins this.
#:
#: Every name here must be in hooks.HOOK_NAMES and none may be in
#: hooks.BLOCKING_HOOKS; tests assert both.
ENTRIES = {
    "sessionStart": "remem hook context --agent cursor",
    "postToolUse": "remem record event --agent cursor",
    "beforeSubmitPrompt": "remem record event --agent cursor",
    "afterAgentResponse": "remem record event --agent cursor",
}


def hooks_path(scope: str, home: Path, cwd: Path) -> Path:
    """The hooks.json for the given scope.

    Both scopes are real, unlike the Claude Code adapter's - Cursor reads
    a user file and a project file, and a per-repository tool has an
    obvious use for the second. An unknown scope raises rather than
    falling back: an install that reports success while having done
    something else is worse than one that refuses.
    """
    if scope == "user":
        return home / ".cursor" / "hooks.json"
    if scope == "project":
        return cwd / ".cursor" / "hooks.json"
    raise UnsupportedScope(
        f"scope '{scope}' is not supported; cursor supports 'user' or 'project'"
    )


def merge(path: Path, entries: dict[str, str]) -> tuple[dict, Path | None]:
    """Merge `entries` into the hooks.json at `path`.

    Returns the merged document and the backup path, or None if there was
    no file to back up.

    Merged rather than overwritten, unlike opencode's remem.js: hooks.json
    is user-owned and other tools legitimately write to it, so remem adds
    its entries beside theirs and never removes one it did not put there.

    Unreadable JSON is treated as an empty document - but only after the
    original bytes are safely in the backup. A corrupt hooks.json must not
    stop the install, and it must not cost the user what they had.
    """
    backup: Path | None = None
    document: dict = {}

    if path.exists():
        raw = path.read_text()
        # Nanosecond resolution, not int(time.time()): two installs inside
        # the same second would otherwise compute the same suffix, and the
        # second write would silently clobber the first backup instead of
        # adding one.
        backup = path.with_suffix(f".json.bak{time.time_ns()}")
        backup.write_text(raw)
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                document = loaded
        except json.JSONDecodeError:
            document = {}

    document["version"] = document.get("version", 1)
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        document["hooks"] = hooks

    for hook, command in entries.items():
        group = hooks.setdefault(hook, [])
        if not isinstance(group, list):
            group = []
            hooks[hook] = group
        # Idempotent by command string: re-running the install must not
        # grow the file, and a hook that fires twice per event would
        # double every row it records.
        if not any(
            isinstance(h, dict) and h.get("command") == command for h in group
        ):
            group.append({"command": command})

    return document, backup
