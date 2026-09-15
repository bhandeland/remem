"""Chunk identity is the repository-relative path, computed by the CLI.

`bag ingest` used to identify a chunk by the path as typed, so `cd docs
&& bag ingest a.md` stored `src:a.md` where a root run stored
`src:docs/a.md` and duplicated every chunk. A rule said to run from the
root; a rule is not a fix. No db marker: these use a scratch git repo
and no store.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from saddlebag.project import toplevel
from saddlebag.services import ingest


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "myrepo"
    (root / "docs" / "deep").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "README.md").write_text("hi\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "init")
    return root.resolve()


def test_toplevel_is_the_working_tree_root_from_a_subdirectory(repo: Path) -> None:
    assert toplevel(repo / "docs" / "deep") == repo


def test_toplevel_of_a_worktree_is_the_worktree_not_the_main_checkout(
    repo: Path, tmp_path: Path
) -> None:
    wt = tmp_path / "somewhere-else"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "wt-branch")
    assert toplevel(wt) == wt.resolve()


def test_toplevel_is_none_outside_a_repository(tmp_path: Path) -> None:
    outside = tmp_path / "plain"
    outside.mkdir()
    assert toplevel(outside) is None


def test_relative_from_the_root_is_unchanged(repo: Path) -> None:
    assert ingest.relative_to_root([Path("docs/a.md")], repo, cwd=repo) == [
        Path("docs/a.md")
    ]


def test_relative_from_a_subdirectory_is_rewritten(repo: Path) -> None:
    assert ingest.relative_to_root([Path("a.md")], repo, cwd=repo / "docs") == [
        Path("docs/a.md")
    ]


def test_absolute_inside_the_repository_is_rewritten(repo: Path) -> None:
    assert ingest.relative_to_root(
        [repo / "docs" / "a.md"], repo, cwd=repo / "docs" / "deep"
    ) == [Path("docs/a.md")]


def test_a_path_outside_the_repository_is_refused(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(
        ingest.BadDesignation, match="outside the repository"
    ) as excinfo:
        ingest.relative_to_root([tmp_path / "elsewhere.md"], repo, cwd=repo)
    # The refusal is unconditional on project scope - --project does not
    # bypass it - so the message must not tell the user to pass a flag
    # that changes nothing.
    assert "--project" not in str(excinfo.value)


def test_a_dot_dot_that_stays_inside_is_fine(repo: Path) -> None:
    assert ingest.relative_to_root([Path("../docs/a.md")], repo, cwd=repo / "docs") == [
        Path("docs/a.md")
    ]
