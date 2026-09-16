"""Which files are transcripts, and whose. Filesystem only - runs on CI."""

from __future__ import annotations

from pathlib import Path

from saddlebag.services import transcripts
from tests.transcript_tree import write_session, write_subagent, write_tool_result


def _ids(directory: Path) -> list[tuple[str, str | None]]:
    return [(f.session_id, f.agent_id) for f in transcripts.transcript_files(directory)]


def test_a_session_file_is_its_own_session_with_no_agent(tmp_path: Path) -> None:
    write_session(tmp_path, "s1")
    assert _ids(tmp_path) == [("s1", None)]


def test_a_subagent_file_belongs_to_the_session_directory_above_it(
    tmp_path: Path,
) -> None:
    """The session id comes from the grandparent directory and the agent id
    from the stem minus `agent-` - which is exactly what every line inside
    a real subagent file says, measured across all 392 on this machine."""
    write_session(tmp_path, "s1")
    path = write_subagent(tmp_path, "s1", "aimpl-task2-ffa7dc6fa2964ee8")
    files = transcripts.transcript_files(tmp_path)
    assert [(f.session_id, f.agent_id) for f in files] == [
        ("s1", None),
        ("s1", "aimpl-task2-ffa7dc6fa2964ee8"),
    ]
    assert files[1].path == path


def test_a_parent_precedes_its_own_subagents(tmp_path: Path) -> None:
    """Path ordering compares parts, so `s1` sorts before `s1.jsonl` and a
    naive `sorted()` would put every subagent ahead of its own parent."""
    write_subagent(tmp_path, "s1", "b")
    write_subagent(tmp_path, "s1", "a")
    write_session(tmp_path, "s1")
    write_session(tmp_path, "s0")
    assert _ids(tmp_path) == [("s0", None), ("s1", None), ("s1", "a"), ("s1", "b")]


def test_one_agent_id_under_two_sessions_is_two_files(tmp_path: Path) -> None:
    """Real: four agent ids on this machine appear under two parents with
    different content. The agent id alone is not an identity."""
    write_subagent(tmp_path, "s1", "aimpl-task3")
    write_subagent(tmp_path, "s2", "aimpl-task3")
    assert _ids(tmp_path) == [("s1", "aimpl-task3"), ("s2", "aimpl-task3")]


def test_a_subagent_whose_parent_file_is_missing_is_still_listed(
    tmp_path: Path,
) -> None:
    """The bytes are the scarce thing. A parent Claude Code has deleted does
    not make its subagents' conversations any less worth keeping."""
    write_subagent(tmp_path, "gone", "a1")
    assert _ids(tmp_path) == [("gone", "a1")]


def test_tool_results_and_memory_are_not_transcripts(tmp_path: Path) -> None:
    write_session(tmp_path, "s1")
    write_tool_result(tmp_path, "s1")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "MEMORY.md").write_text("- x\n")
    assert _ids(tmp_path) == [("s1", None)]


def test_a_missing_directory_has_no_transcripts(tmp_path: Path) -> None:
    assert transcripts.transcript_files(tmp_path / "absent") == []
