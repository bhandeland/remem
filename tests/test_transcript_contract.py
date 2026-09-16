"""Two tests, deliberately not one.

The first always runs, on CI and everywhere else, and catches a parser that
breaks on a shape we already know about. The second may skip, and only
guards the freshness of our understanding of a format Claude Code changes
without telling us. Collapsing them would produce a guard that skips on CI -
the failure mode the db markers already taught this project to distrust.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from saddlebag.transcript_file import parse

FIXTURE = Path(__file__).parent / "fixtures" / "transcript-sample.jsonl"

#: Every line type the fixture covers, which is every type observed in a real
#: transcript as of 2026-09-15. A type NOT in here is not an error - the
#: parser nulls the column and stores the line - but it is worth knowing.
KNOWN_TYPES = {
    "assistant",
    "user",
    "attachment",
    "system",
    "last-prompt",
    "mode",
    "permission-mode",
    "atis-latch",
    "ai-title",
    "file-history-snapshot",
    "file-history-delta",
    "cost-state",
    "queue-operation",
    "progress",
}


def test_the_parser_handles_every_line_type_we_have_seen() -> None:
    content = FIXTURE.read_bytes()
    lines, failures = parse(content)
    real = sum(1 for line in content.split(b"\n") if line.strip())
    assert len(lines) + len(failures) == real
    assert {line.type for line in lines if line.type} <= KNOWN_TYPES


@pytest.mark.transcript
def test_a_real_transcript_still_parses(tmp_path: Path) -> None:
    """May skip. Claude Code changes its format without telling us.

    Expect this to fire eventually, the way opencode's freshness test did
    mid-branch. When it does, the fix is to add the new type to KNOWN_TYPES
    and to the fixture - not to make the parser stricter.
    """
    root = Path.home() / ".claude" / "projects"
    found: list[Path] = sorted(root.glob("*/*.jsonl")) if root.is_dir() else []
    if not found:
        pytest.skip(f"no transcript under {root}")
    lines, failures = parse(found[-1].read_bytes())
    assert lines, "a real transcript parsed to no lines at all"
    unknown = {line.type for line in lines if line.type} - KNOWN_TYPES
    assert not unknown, f"new line types: {sorted(unknown)}"
