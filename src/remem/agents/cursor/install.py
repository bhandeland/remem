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


#: Commands a previous remem wrote for a hook, which install migrates in
#: place. Empty because no cursor command has been renamed yet - it is here
#: so that the first rename is a one-line edit rather than a bug.
#:
#: The Claude Code adapter learned this the expensive way: `remem hook
#: session-end` was superseded by `remem hook record-event`, the membership
#: test below is by exact string, so installing over an older settings.json
#: appended the new command beside the old one and both fired. `events` has
#: no unique constraint, so every session close wrote a duplicate row for
#: the extractor to read twice.
LEGACY_COMMANDS: dict[str, tuple[str, ...]] = {}


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


def merge(
    path: Path,
    entries: dict[str, str],
    legacy: dict[str, tuple[str, ...]] | None = None,
) -> tuple[dict, Path | None]:
    """Merge `entries` into the hooks.json at `path`.

    Returns the merged document and the backup path, or None if there was
    no file to back up.

    Merged rather than overwritten, unlike opencode's remem.js: hooks.json
    is user-owned and other tools legitimately write to it, so remem adds
    its entries beside theirs and never removes one it did not put there.

    Unreadable JSON is treated as an empty document - but only after the
    original bytes are safely in the backup. A corrupt hooks.json must not
    stop the install, and it must not cost the user what they had.

    `legacy` names commands this install supersedes, hook to old commands;
    it defaults to LEGACY_COMMANDS and is a parameter so a test can exercise
    the migration while that table is still empty.
    """
    legacy = LEGACY_COMMANDS if legacy is None else legacy
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
        # Repair remem's own entries before testing membership. Idempotence
        # alone only ever prevented a duplicate THIS install would add; it
        # never fixed one already in the file, and it never noticed an entry
        # naming a command remem has since renamed. A hook that fires twice
        # doubles every row it records.
        #
        # Scoped to the commands remem writes: hooks.json is shared and
        # user-owned, so another tool's entries - duplicates included - are
        # left exactly as found. Removing one would be worse than the
        # duplicate this is fixing.
        ours = tuple(legacy.get(hook, ())) + (command,)
        kept: list = []
        seen = False
        for h in group:
            if not isinstance(h, dict) or h.get("command") not in ours:
                kept.append(h)
                continue
            if seen:
                continue
            seen = True
            # Rewritten in place rather than replaced, so per-entry options
            # Cursor may grow survive the migration.
            h["command"] = command
            kept.append(h)
        if not seen:
            kept.append({"command": command})
        group[:] = kept

    return document, backup
