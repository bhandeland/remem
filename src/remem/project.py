"""Which project a write belongs to.

Resolved from the git repository rather than the current directory's name.
The directory name is wrong in two ordinary cases: working in a subdirectory
(`cd src` filed entries under a project called "src") and working in a git
worktree (filed under the worktree's directory rather than the repository's).
Both failed silently - the write succeeded and the entry simply never appeared
in the knowledge base, which queries on project.

Never raises: a project name is a convenience, not a dependency. Without git,
or outside a repository, the directory name is a reasonable answer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _repo_root(start: Path) -> Path | None:
    """The MAIN repository's root, even from inside a worktree.

    `--git-common-dir` points at the shared .git directory - for a worktree
    that is the main repository's, which is what makes a worktree resolve to
    the same project as the repository it belongs to.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = start / common
    try:
        return common.resolve().parent
    except OSError:
        return None


def resolve_project(start: Path | None = None) -> str | None:
    """The project name for a directory, or None when there isn't one."""
    start = (start or Path.cwd()).resolve()
    root = _repo_root(start)
    name = (root or start).name
    return name or None


def toplevel(start: Path | None = None) -> Path | None:
    """The WORKING TREE's top level, or None outside a repository.

    Not `repo_root`, which resolves a worktree to the main checkout: a
    file inside a worktree is not under the main checkout's root, so a
    path relative to it cannot be computed. The relative path is the same
    against either, which is what makes a worktree ingest and a
    main-checkout ingest of the same file the same identity - the same
    property `resolve_project` gives the project name. Resolved, so a
    caller can `relative_to` it against a resolved path (macOS /tmp is a
    symlink to /private/tmp, and `relative_to` is textual).
    """
    start = (start or Path.cwd()).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return Path(result.stdout.strip()).resolve()
    except OSError:
        return None


def repo_root(start: Path | None = None) -> Path:
    """The directory a project-scoped write should land in.

    The repository root when `start` is inside one (a worktree resolves to
    the *main* repository's root, same as `resolve_project`), otherwise
    `start` itself - a project-scoped install outside any repository still
    has to write somewhere, and falling back to the given directory rather
    than raising is what `resolve_project`'s docstring means by "a project
    name is a convenience, not a dependency".
    """
    start = (start or Path.cwd()).resolve()
    return _repo_root(start) or start
