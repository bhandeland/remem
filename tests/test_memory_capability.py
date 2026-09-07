from __future__ import annotations

from pathlib import Path

from remem.agents.claude_code import memory as cc_memory
from remem.agents.registry import get


def test_the_slug_is_the_absolute_path_with_separators_replaced():
    assert cc_memory.slug_for(Path("/Users/brandon/llmworkspace/remem")) == (
        "-Users-brandon-llmworkspace-remem"
    )


def test_two_checkouts_of_one_repo_share_a_project_name_but_not_a_slug():
    # remem resolves a project through --git-common-dir, so both of these
    # are project "remem". Claude Code keys its directory on the whole path,
    # so the slugs must differ - which is the entire reason memory_dir takes
    # a path and not a project name. A slug built from the directory's name
    # would collide here and silently hand two checkouts one memory store.
    a = cc_memory.slug_for(Path("/Users/b/work/remem"))
    b = cc_memory.slug_for(Path("/Users/b/other/remem"))
    assert a != b
    assert a.endswith("-work-remem")
    assert b.endswith("-other-remem")


def test_memory_dir_sits_under_the_claude_home():
    got = cc_memory.memory_dir(
        Path("/w/proj"),
        env={"CLAUDE_CONFIG_DIR": "/cfg"},
    )
    assert got == Path("/cfg/projects/-w-proj/memory")


def test_memory_dir_defaults_to_dot_claude_in_home(tmp_path):
    # `home` is an explicit parameter, the same shape settings_path/
    # hook_state/install already take one - not read out of `env`, which
    # would leave a caller with no way to control it (Path("~").expanduser()
    # reads the real os.environ, ignoring whatever HOME an `env` mapping
    # names).
    got = cc_memory.memory_dir(Path("/w/proj"), home=tmp_path, env={})
    assert got == tmp_path / ".claude" / "projects" / "-w-proj" / "memory"


def test_memory_dir_falls_back_to_the_real_home_when_none_is_given():
    got = cc_memory.memory_dir(Path("/w/proj"), env={})
    assert got == Path.home() / ".claude" / "projects" / "-w-proj" / "memory"


def test_the_adapter_exposes_the_capability():
    # get() returns the adapter class, not an instance (see
    # test_agents_registry.py::test_get_returns_the_adapter_class) - every
    # existing caller instantiates before calling a method (env_vars.py's
    # test does `ClaudeCodeAdapter().env_settings()`; doctor.py does
    # `adapter() if isinstance(adapter, type) else adapter`), so this test
    # follows the same convention rather than calling an unbound method.
    adapter = get("claude-code")()
    assert getattr(adapter, "memory_dir", None) is not None
    assert adapter.memory_dir(
        Path("/w/proj"),
        env={"CLAUDE_CONFIG_DIR": "/cfg"},
    ) == Path("/cfg/projects/-w-proj/memory")


def test_another_adapter_does_not_pretend_to_have_it():
    # A probed capability: opencode has no such directory, and reporting one
    # would send the sync to a path nothing reads.
    assert getattr(get("opencode")(), "memory_dir", None) is None
