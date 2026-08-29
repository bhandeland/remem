"""Where the opencode plugin goes.

Split out of adapter.py because it is the one piece of this adapter with a
Claude Code analogue worth contrasting directly: `resolve_paths` in
`agents/claude_code/adapter.py` resolves one root, relocatable with
CLAUDE_CONFIG_DIR. This resolves between two roots depending on scope, and
neither is relocatable - see the comment below for why.
"""

from __future__ import annotations

from pathlib import Path

from remem.agents.base import UnsupportedScope
from remem.project import repo_root


def plugin_dir(scope: str, home: Path, cwd: Path) -> Path:
    """The directory opencode scans for plugins, for the given scope.

    `home` and `cwd` are both parameters, not read internally, for the same
    reason `install()` takes `env` rather than reading `os.environ`: tests
    relocate them without touching the real process.
    """
    # Deliberately no CLAUDE_CONFIG_DIR-style override here. Claude Code
    # publishes an environment variable that relocates its config root, so
    # honouring it in remem's installer keeps both tools pointed at the same
    # place. opencode has no documented equivalent - inventing one would
    # give remem a knob opencode itself does not read, which is worse than
    # no knob: a user who set it would have every reason to believe it did
    # something.
    if scope == "user":
        return home / ".config" / "opencode" / "plugin"
    if scope == "project":
        # `repo_root`, not raw `cwd`: opencode is the first adapter with a
        # project scope at all, and a project-scoped install run from a
        # subdirectory has to land beside the repository, not beside the
        # subdirectory, or opencode never finds the plugin it just wrote.
        # This is the same resolution `identity()` already does for writes
        # (via `resolve_project`), extended to cover where the file itself
        # goes. `repo_root` falls back to `cwd` outside a repository rather
        # than raising - a plugin directory in a non-repo project directory
        # is still meaningful.
        return repo_root(cwd) / ".opencode" / "plugin"
    raise UnsupportedScope(
        f"scope '{scope}' is not supported; opencode supports 'user' or 'project'"
    )
