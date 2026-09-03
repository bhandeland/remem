"""Where Claude Code keeps its file-based memory.

A fact about Claude Code, so it lives on the adapter rather than in
services/ - the same reason env_vars.py does. A second harness with a
memory directory would ship its own.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def slug_for(cwd: Path) -> str:
    """Claude Code's slug for a working directory: separators to hyphens.

    Note this is the *working directory*, not remem's project. They are not
    derivable from one another: remem resolves a project through
    --git-common-dir so a worktree shares the parent repository's name,
    while Claude Code keys this directory on the path alone. remem's own
    development happens in a worktree, so the two differ here routinely.
    """
    return str(Path(cwd).resolve()).replace(os.sep, "-")


def memory_dir(cwd: Path, env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    home = env.get("CLAUDE_CONFIG_DIR")
    root = Path(home) if home else Path(env.get("HOME", "~")).expanduser() / ".claude"
    return root / "projects" / slug_for(cwd) / "memory"
