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

    Note this is the *working directory*, not saddlebag's project. They are not
    derivable from one another: saddlebag resolves a project through
    --git-common-dir so a worktree shares the parent repository's name,
    while Claude Code keys this directory on the path alone. saddlebag's own
    development happens in a worktree, so the two differ here routinely.
    """
    return str(Path(cwd).resolve()).replace(os.sep, "-")


def projects_dir(
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Claude Code's per-working-directory tree: memory, and transcripts.

    Factored out of `memory_dir` because a second caller appeared -
    transcript capture reads `<config>/projects/<slug>/*.jsonl` out of the
    same tree. `CLAUDE_CONFIG_DIR` moves the whole thing, so a caller that
    builds `~/.claude/projects` by hand is silently wrong for anyone who
    sets it: discovery proposes nothing and finds no evidence to say why.
    One resolution, here, is what stops the two answers drifting.
    """
    env = os.environ if env is None else env
    configured = env.get("CLAUDE_CONFIG_DIR")
    root = Path(configured) if configured else (home or Path.home()) / ".claude"
    return root / "projects"


def memory_dir(
    cwd: Path,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Where saddlebag's generated view of Claude Code's memory directory lives.

    Takes `home` as an explicit parameter, the same shape every other
    adapter capability that needs one uses (`settings_path`, `hook_state`,
    `install`), rather than digging `HOME` out of `env`. `Path("~").expanduser()`
    reads the *real* `os.environ`, not whatever mapping a caller passes as
    `env` - so a test or caller that wants a different home has no way to
    get one through `env` alone, and would silently get the real HOME
    instead. Taking `home` removes that trap rather than documenting it.
    """
    return projects_dir(home, env) / slug_for(cwd) / "memory"
