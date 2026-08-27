"""Which project a write belongs to.

Resolved from the git repository, not the current directory's name. Using the
directory name meant `cd src && remem rule ...` filed the rule under project
"src" - silently somewhere nothing would look for it - and a git worktree filed
under the worktree's directory name rather than the repository's.
"""

import subprocess

import pytest

from remem.project import resolve_project


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True,
                   capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "myrepo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "README.md").write_text("hi\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "init")
    return root


def test_the_repository_root_resolves_to_the_repository_name(repo):
    assert resolve_project(repo) == "myrepo"


def test_a_subdirectory_resolves_to_the_repository_not_the_subdirectory(repo):
    """The case that made this a silent-data bug rather than a quirk: working
    in src/ is ordinary, and filed rules under a project called "src"."""
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    assert resolve_project(sub) == "myrepo"


def test_a_worktree_resolves_to_the_main_repository(repo, tmp_path):
    """A worktree is the same project, whatever its directory is called."""
    wt = tmp_path / "somewhere-else"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "wt-branch")
    assert resolve_project(wt) == "myrepo"


def test_outside_a_repository_it_falls_back_to_the_directory_name(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert resolve_project(plain) == "not-a-repo"


def test_a_missing_git_binary_falls_back_rather_than_raising(tmp_path, monkeypatch):
    """remem must work without git installed; project is a convenience, not a
    dependency."""
    def boom(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", boom)
    d = tmp_path / "plainly-named"
    d.mkdir()
    assert resolve_project(d) == "plainly-named"


def test_a_git_failure_falls_back_rather_than_raising(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("something went wrong")

    monkeypatch.setattr(subprocess, "run", boom)
    d = tmp_path / "fallback-name"
    d.mkdir()
    assert resolve_project(d) == "fallback-name"


def test_the_adapter_resolves_the_payload_cwd_through_the_repository(repo):
    """The hook derives both the knowledge-base slug and the capture project
    from this. Using the payload's directory name meant a session started in a
    subdirectory or a worktree injected nothing and captured under the wrong
    project - and the hook, being fail-soft, said nothing about it."""
    from remem.agents.claude_code.adapter import ClaudeCodeAdapter

    sub = repo / "src"
    sub.mkdir()
    identity = ClaudeCodeAdapter().identity({}, {"cwd": str(sub)})
    assert identity.project == "myrepo"


def test_the_adapter_still_handles_a_payload_without_a_cwd():
    from remem.agents.claude_code.adapter import ClaudeCodeAdapter

    assert ClaudeCodeAdapter().identity({}, {}).project is None


def test_the_filesystem_root_yields_no_project(tmp_path, monkeypatch):
    """`/` has no meaningful name; None means global rather than a project
    called empty-string."""
    from pathlib import Path

    assert resolve_project(Path("/")) is None
