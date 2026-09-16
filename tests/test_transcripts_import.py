from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import (
    Event,
    EventKind,
    Principal,
    TranscriptTrigger,
    new_id,
)
from saddlebag.services import transcripts
from tests.conftest import found
from tests.transcript_tree import (
    write_session,
    write_subagent,
    write_subagent_meta,
    write_tool_result,
)

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


def _write(directory: Path, session_id: str, lines: list[dict[str, Any]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{session_id}.jsonl"
    target.write_bytes(b"".join(json.dumps(line).encode() + b"\n" for line in lines))
    return target


def test_import_stores_content_and_lines(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    _write(tmp_path, "s1", [{"type": "user"}, {"type": "assistant"}])
    transcripts.designate(store, owner.id, "p", tmp_path)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_new == 1
    assert report.lines_written == 2
    stored = found(
        store.get_transcript(owner.id, transcripts.HARNESS, "s1", agent_id=None)
    )
    assert store.transcript_line_count(stored.id) == 2


def test_a_second_run_over_an_unchanged_file_reads_nothing(
    store: PostgresStore,
    owner: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common case at every session start: one stat, no read.

    Asserting counters alone lets an implementation that reads the whole
    file and compares hashes pass this test too - the counters end up right
    either way. Making `Path.open` and `Path.read_bytes` raise for the
    second run turns "did not read" from an inference into something that
    fails loudly the moment it is violated.
    """
    _write(tmp_path, "s1", [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    def _must_not_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("an unchanged file must not be read")

    monkeypatch.setattr(Path, "open", _must_not_read)
    monkeypatch.setattr(Path, "read_bytes", _must_not_read)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_seen == 1
    assert report.files_new == 0
    assert report.files_appended == 0
    assert report.lines_written == 0


def test_an_appended_file_adds_only_the_new_lines(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    target = _write(tmp_path, "s1", [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    with target.open("ab") as fh:
        fh.write(json.dumps({"type": "assistant"}).encode() + b"\n")

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_appended == 1
    assert report.lines_written == 1
    stored = found(
        store.get_transcript(owner.id, transcripts.HARNESS, "s1", agent_id=None)
    )
    assert store.transcript_line_count(stored.id) == 2


def test_append_keeps_seq_aligned_with_the_file_across_a_bad_line(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """seq is the line's number in the FILE, not the count of rows stored.

    A line that fails to parse still occupies a line. If the append path
    derived its starting seq from the stored ROW count, every blank or
    unparseable line would shift every later seq by one - silently, and
    against the coordinate future labelling work keys on.
    """
    target = tmp_path / "s1.jsonl"
    target.write_bytes(b'{"type": "user"}\nthis is not json\n')
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    with target.open("ab") as fh:
        fh.write(b'{"type": "assistant"}\n')

    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    stored = found(
        store.get_transcript(owner.id, transcripts.HARNESS, "s1", agent_id=None)
    )
    assert store.transcript_line_count(stored.id) == 2
    # The appended line is the file's THIRD line, seq 2 - not seq 1, which is
    # what a row-count-derived start would have produced.
    with store._cur() as cur:  # noqa: SLF001 - asserting stored seqs directly
        cur.execute(
            "select seq from transcript_lines where transcript_id = %s order by seq",
            (stored.id,),
        )
        assert [r["seq"] for r in cur.fetchall()] == [0, 2]


def test_a_rewritten_file_rebuilds_its_lines(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    _write(tmp_path, "s1", [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    # Same session, different history, longer than before.
    _write(tmp_path, "s1", [{"type": "system"}, {"type": "assistant"}])

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_rebuilt == 1
    stored = found(
        store.get_transcript(owner.id, transcripts.HARNESS, "s1", agent_id=None)
    )
    assert store.transcript_line_count(stored.id) == 2


def test_a_shrunk_file_is_an_anomaly_and_is_not_followed(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """The stored copy is more complete. Two sessions are already gone from
    disk, and this is the rule that exists for exactly that."""
    _write(tmp_path, "s1", [{"type": "user"}, {"type": "assistant"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)
    before = found(
        store.get_transcript(owner.id, transcripts.HARNESS, "s1", agent_id=None)
    )

    _write(tmp_path, "s1", [{"type": "user"}])

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert len(report.anomalies) == 1
    assert report.anomalies[0]["stored"] == before.bytes
    after = found(
        store.get_transcript(owner.id, transcripts.HARNESS, "s1", agent_id=None)
    )
    assert after.bytes == before.bytes


def test_an_unparseable_line_is_named_not_dropped(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    tmp_path.joinpath("s1.jsonl").write_bytes(b'{"type": "user"}\nthis is not json\n')
    transcripts.designate(store, owner.id, "p", tmp_path)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.lines_written == 1
    assert len(report.failures) == 1
    assert (
        "not json" in report.failures[0]["reason"]
        or "line 1" in (report.failures[0]["reason"])
    )


def test_the_cap_bounds_a_refresh_before_it_reads(
    store: PostgresStore,
    owner: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session-start hook must never read 179MB.

    Enforced before reading, not after, which is the whole point. Asserting
    `files_new == 2` alone is satisfied just as well by a check-after-read
    loop that processes all 5 files and only afterward reports 2 of them -
    the counter looks right either way. Counting real `Path.stat` calls on
    the candidate files is what tells the two implementations apart: the
    cap must stop the loop before it ever touches files 3 through 5, not
    merely before it counts them.
    """
    for i in range(5):
        _write(tmp_path, f"s{i}", [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)

    stat_calls = 0
    real_stat = Path.stat

    def _counting_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        nonlocal stat_calls
        if self.suffix == ".jsonl":
            stat_calls += 1
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", _counting_stat)

    report = transcripts.run(
        store, owner.id, "p", trigger=TranscriptTrigger.AUTO, cap=2
    )

    assert report.files_new == 2
    assert report.files_seen == 2
    assert stat_calls == 2


def test_an_unclaimed_project_does_nothing(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """The common case, and the entire opt-in.

    Asserting `files_seen == 0` alone is satisfied just as well by a version
    that still writes an empty run row - and `bag transcripts refresh` is
    spawned at every session start for every project, so that version would
    leave a `transcript_runs` row per session per unclaimed project, forever.
    Asserting no row exists is what would have caught it.
    """
    _write(tmp_path, "s1", [{"type": "user"}])
    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.AUTO)
    assert report.files_seen == 0
    assert store.latest_transcript_run(owner.id, "p") is None


def test_a_claimed_project_records_a_run_even_with_nothing_to_import(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """Opting in means your runs are visible, even the empty ones.

    The early return is for projects that never claimed a directory. A
    project that claimed one and simply has no new files is a different
    thing, and a reader needs to see that it ran.
    """
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)
    assert store.latest_transcript_run(owner.id, "p") is not None


def test_a_missing_claimed_directory_is_a_failure_not_a_crash(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    transcripts.designate(store, owner.id, "p", tmp_path)
    tmp_path.rmdir()
    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)
    assert len(report.failures) == 1


def test_a_run_of_failing_files_is_raised_and_the_row_still_finishes(
    store: PostgresStore,
    owner: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run()`'s wrapper records a partial run and re-raises.

    One failing file is that file's failure (see the next test). Several in
    a row mean the problem is not the files - a dropped connection fails
    every one of them - so the run stops and raises rather than recording
    the same error hundreds of times. The row still finishes, carrying the
    per-file failures and the `{"path": "*"}` one.

    What this does NOT cover: a real psycopg error under a non-autocommit
    session, where the `finish_transcript_run` in the `finally` would itself
    raise `InFailedSqlTransaction`. The per-test `conn` fixture is one
    rolled-back transaction and cannot show that; the CLI's own tests
    establish the `autocommit=True` contract end to end.
    """
    for session_id in ("s1", "s2", "s3", "s4"):
        _write(tmp_path, session_id, [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(store, "put_transcript", _boom)

    with pytest.raises(RuntimeError, match="simulated failure"):
        transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    run = found(store.latest_transcript_run(owner.id, "p"))
    assert run.finished_at is not None
    assert [f["path"] for f in run.failures] == [
        str(tmp_path / "s1.jsonl"),
        str(tmp_path / "s2.jsonl"),
        str(tmp_path / "s3.jsonl"),
        "*",
    ]
    assert run.files_seen == 3


def test_one_failing_file_does_not_stop_the_others(
    store: PostgresStore,
    owner: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file that raises every run used to stop its project's imports for
    good. It is now that file's failure, recorded every run, and the files
    after it still import. Interleaved with successes, failures never add
    up to the consecutive limit."""
    # Files import in name order, so the names fix the interleaving.
    for session_id in ("a-bad", "b-bad", "c-good", "d-bad", "e-bad", "f-good"):
        _write(tmp_path, session_id, [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)

    real = store.put_transcript

    def _flaky(*args: Any, **kwargs: Any) -> Any:
        if args[3].endswith("-bad"):
            raise RuntimeError(f"cannot read {args[3]}")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "put_transcript", _flaky)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_new == 2
    assert [f["path"] for f in report.failures] == [
        str(tmp_path / f"{s}.jsonl") for s in ("a-bad", "b-bad", "d-bad", "e-bad")
    ]
    assert report.failures[0]["reason"] == "RuntimeError: cannot read a-bad"
    monkeypatch.undo()
    assert found(
        store.get_transcript(owner.id, transcripts.HARNESS, "f-good", agent_id=None)
    )


def test_the_run_is_recorded_with_its_trigger(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    _write(tmp_path, "s1", [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.AUTO)
    run = found(store.latest_transcript_run(owner.id, "p"))
    assert run.trigger is TranscriptTrigger.AUTO
    assert run.finished_at is not None


def _record(store: PostgresStore, owner: Principal, project: str, session: str) -> None:
    """One recorded event, which is all the project-agreement check reads."""
    store.put_event(
        Event(
            id=new_id(),
            owner_id=owner.id,
            project=project,
            harness="claude-code",
            session_id=session,
            kind=EventKind.TOOL_CALL,
            tool="Bash",
            payload={"command": "ls"},
            occurred_at=datetime(2026, 9, 15, tzinfo=UTC),
        )
    )


def test_a_transcript_whose_derived_lines_vanished_is_rebuilt(
    store: PostgresStore,
    owner: Principal,
    conn: psycopg.Connection[Any],
    tmp_path: Path,
) -> None:
    """The bytes and the lines are written in separate transactions.

    Both CLI paths open with autocommit=True - the run row needs it - so a
    Ctrl-C during a long typed backfill, or a line Postgres refuses as
    jsonb, can leave the content stored and `transcript_lines` empty.
    Nothing would ever notice on its own: the file has not changed, so every
    later run stats it, classifies SKIP, and it stays empty forever.

    The rows are deleted here directly rather than by simulating a crash,
    because the stranded STATE is what the repair keys on and how it came
    about does not matter. Reverting the guard leaves the count at zero.
    """
    _write(tmp_path, "s1", [{"type": "user"}, {"type": "assistant"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)
    stored = found(
        store.get_transcript(owner.id, transcripts.HARNESS, "s1", agent_id=None)
    )

    with conn.cursor() as cur:
        cur.execute(
            "delete from transcript_lines where transcript_id = %s", (stored.id,)
        )
    assert store.transcript_line_count(stored.id) == 0

    # The file on disk is untouched, so this is the SKIP path - the only
    # thing that can bring the lines back is the count guard.
    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_rebuilt == 1
    assert store.transcript_line_count(stored.id) == 2


@pytest.mark.parametrize(
    "content", [b"", b"not json\n", b"[1, 2]\n"], ids=["empty", "junk", "non-object"]
)
def test_a_file_with_no_lines_to_derive_is_not_repaired_every_run(
    store: PostgresStore, owner: Principal, tmp_path: Path, content: bytes
) -> None:
    """Zero stored lines is the repair's trigger, and some files legitimately
    have zero lines - an empty file, or one no line of which is a JSON
    object. Rebuilding those can never produce a row, so the repair fired on
    every refresh, re-reported the same line failures, and spent one of the
    cap's files each time. Here it would spend the only one, and `b-new`
    would never import."""
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "a-inert.jsonl").write_bytes(content)
    transcripts.designate(store, owner.id, "p", tmp_path)
    first = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)
    assert first.files_new == 1

    _write(tmp_path, "b-new", [{"type": "user"}])
    report = transcripts.run(
        store, owner.id, "p", trigger=TranscriptTrigger.AUTO, cap=1
    )

    assert (report.files_new, report.files_rebuilt) == (1, 0)
    assert report.failures == []
    assert found(
        store.get_transcript(owner.id, transcripts.HARNESS, "b-new", agent_id=None)
    )


def test_an_unchanged_file_costs_no_query_of_its_own(
    store: PostgresStore,
    owner: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh runs at every session start over hundreds of unchanged
    files. It used to fetch each one's row and then count its lines - two
    queries a file, ~700 a run for one claimed directory. The rows are now
    read once per run, carrying whether their lines exist."""
    for session_id in ("s1", "s2"):
        _write(tmp_path, session_id, [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("queried per file")

    monkeypatch.setattr(store, "get_transcript", _boom)
    monkeypatch.setattr(store, "transcript_line_count", _boom)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.AUTO)

    assert report.failures == []
    assert report.files_seen == 2


def test_a_session_recorded_under_another_project_is_an_anomaly_and_is_still_stored(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """The spec's project-agreement check, and what it deliberately does not do.

    A directory can hold sessions from more than one project if a working
    directory moved. The disagreement is reported, and the bytes are stored
    anyway: they are the scarce thing - a session Claude Code has since
    deleted cannot be fetched again - and refusing to store them to protect
    a label would trade the irreplaceable half for the repairable one.
    """
    _record(store, owner, "B", "s1")
    _write(tmp_path, "s1", [{"type": "user"}])
    transcripts.designate(store, owner.id, "A", tmp_path)

    report = transcripts.run(store, owner.id, "A", trigger=TranscriptTrigger.MANUAL)

    assert len(report.anomalies) == 1
    anomaly = report.anomalies[0]
    assert anomaly["reason"] == transcripts.PROJECT_CONFLICT
    assert anomaly["claiming"] == "A"
    assert anomaly["recorded"] == ["B"]
    assert report.files_new == 1
    assert (
        found(
            store.get_transcript(owner.id, transcripts.HARNESS, "s1", agent_id=None)
        ).bytes
        > 0
    )


def test_a_session_recorded_under_the_claiming_project_is_not_an_anomaly(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """The common case, and the one that would make the check useless noise.

    Most files in a claimed directory have no recorded events at all -
    sessions predating the pipeline - and the rest agree. Only a recorded
    project that DISAGREES is worth a line.
    """
    _record(store, owner, "A", "s1")
    _write(tmp_path, "s1", [{"type": "user"}])
    _write(tmp_path, "never-recorded", [{"type": "user"}])
    transcripts.designate(store, owner.id, "A", tmp_path)

    report = transcripts.run(store, owner.id, "A", trigger=TranscriptTrigger.MANUAL)

    assert report.anomalies == []
    assert report.files_new == 2


def test_an_import_stores_every_subagent_file_byte_exact(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """The gap this amendment closes: 392 files and 131MB on the machine it
    was measured on, and the only copy of those conversations."""
    write_session(tmp_path, "s1")
    a1 = write_subagent(tmp_path, "s1", "a1", [{"type": "user"}, {"type": "assistant"}])
    write_subagent(tmp_path, "s1", "a2")
    transcripts.designate(store, owner.id, "p", tmp_path)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_new == 3
    parent = found(
        store.get_transcript(owner.id, transcripts.HARNESS, "s1", agent_id=None)
    )
    child = found(
        store.get_transcript(owner.id, transcripts.HARNESS, "s1", agent_id="a1")
    )
    assert parent.id != child.id
    assert store.transcript_content(child.id, owner.id) == a1.read_bytes()
    # designate stores the resolved claim path, and macOS tmp paths may
    # differ in spelling (/tmp vs /private/tmp) - compare as resolved Paths
    # rather than strings.
    assert Path(child.path) == a1.resolve()
    assert store.transcript_line_count(child.id) == 2
    # Identity came from the path's directories, never from its stem.
    assert (
        store.get_transcript(owner.id, transcripts.HARNESS, "agent-a1", agent_id=None)
        is None
    )


def test_one_agent_id_under_two_sessions_is_stored_twice(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    write_subagent(tmp_path, "s1", "a1", [{"type": "user"}])
    write_subagent(tmp_path, "s2", "a1", [{"type": "assistant"}, {"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_new == 2
    one = found(
        store.get_transcript(owner.id, transcripts.HARNESS, "s1", agent_id="a1")
    )
    two = found(
        store.get_transcript(owner.id, transcripts.HARNESS, "s2", agent_id="a1")
    )
    assert store.transcript_line_count(one.id) == 1
    assert store.transcript_line_count(two.id) == 2


def test_a_subagent_whose_parent_file_is_missing_is_still_stored(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """Not an anomaly: nothing about the file is suspect. The missing parent
    is what `irrecoverable` reports."""
    write_subagent(tmp_path, "gone", "a1")
    transcripts.designate(store, owner.id, "p", tmp_path)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_new == 1
    assert report.anomalies == []
    assert store.get_transcript(owner.id, transcripts.HARNESS, "gone", agent_id="a1")


def test_an_unchanged_subagent_file_is_not_read_again(
    store: PostgresStore,
    owner: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lookup that ignored the agent would find the PARENT's row, see a
    different size, and read the file - so disabling reads is what proves
    each file is matched to its own row."""
    write_session(tmp_path, "s1", [{"type": "user"}, {"type": "assistant"}])
    write_subagent(tmp_path, "s1", "a1")
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    def _must_not_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("an unchanged file must not be read")

    monkeypatch.setattr(Path, "open", _must_not_read)
    monkeypatch.setattr(Path, "read_bytes", _must_not_read)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_seen == 2
    assert report.files_new == 0
    assert report.files_appended == 0
    assert report.files_rebuilt == 0


def test_an_appended_subagent_file_adds_only_its_new_lines(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    path = write_subagent(tmp_path, "s1", "a1", [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    with path.open("ab") as fh:
        fh.write(json.dumps({"type": "assistant"}).encode() + b"\n")
    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_appended == 1
    assert report.lines_written == 1
    child = found(
        store.get_transcript(owner.id, transcripts.HARNESS, "s1", agent_id="a1")
    )
    assert store.transcript_content(child.id, owner.id) == path.read_bytes()


def test_tool_results_are_not_imported(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    write_session(tmp_path, "s1")
    write_tool_result(tmp_path, "s1")
    transcripts.designate(store, owner.id, "p", tmp_path)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_seen == 1


def test_a_subagent_of_a_session_recorded_elsewhere_is_an_anomaly_too(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """Looking the session up by the file's stem would find `agent-a1` in
    no recorded project and skip the check for every subagent, silently."""
    _record(store, owner, "B", "s1")
    write_session(tmp_path, "s1")
    write_subagent(tmp_path, "s1", "a1")
    write_subagent(tmp_path, "s1", "a2")
    transcripts.designate(store, owner.id, "A", tmp_path)

    report = transcripts.run(store, owner.id, "A", trigger=TranscriptTrigger.MANUAL)

    assert report.files_new == 3
    assert {(a["session_id"], a["agent_id"]) for a in report.anomalies} == {
        ("s1", None),
        ("s1", "a1"),
        ("s1", "a2"),
    }
    assert all(a["reason"] == transcripts.PROJECT_CONFLICT for a in report.anomalies)


def _subagent_row(store: PostgresStore, owner: Principal, agent_id: str) -> Any:
    return found(
        store.get_transcript(owner.id, transcripts.HARNESS, "s1", agent_id=agent_id)
    )


def test_a_new_subagent_stores_its_sidecar_byte_for_byte(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    write_session(tmp_path, "s1")
    write_subagent(tmp_path, "s1", "a1")
    sidecar = write_subagent_meta(tmp_path, "s1", "a1")
    transcripts.designate(store, owner.id, "p", tmp_path)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.metas_written == 1
    row = _subagent_row(store, owner, "a1")
    assert store.transcript_meta(row.id, owner.id) == sidecar.read_bytes()
    run = found(store.latest_transcript_run(owner.id, "p"))
    assert run.metas_written == 1


def test_a_row_imported_before_its_sidecar_picks_it_up_unchanged(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """The backfill. Every row imported before 025 classifies SKIP on its
    transcript, so a sidecar check living only on the new/append paths
    would never reach one of them."""
    write_subagent(tmp_path, "s1", "a1")
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)
    assert _subagent_row(store, owner, "a1").has_meta is False

    sidecar = write_subagent_meta(tmp_path, "s1", "a1")
    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert (report.files_new, report.files_appended, report.files_rebuilt) == (0, 0, 0)
    assert report.metas_written == 1
    row = _subagent_row(store, owner, "a1")
    assert store.transcript_meta(row.id, owner.id) == sidecar.read_bytes()


def test_a_stored_sidecar_is_never_read_again(
    store: PostgresStore,
    owner: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Write-once on disk, so read-once here - and the unchanged-file
    promise (one stat, no read) still holds for a subagent with a sidecar."""
    write_subagent(tmp_path, "s1", "a1")
    original = write_subagent_meta(tmp_path, "s1", "a1").read_bytes()
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    write_subagent_meta(tmp_path, "s1", "a1", b'{"agentType":"changed"}')

    def _must_not_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("a stored sidecar must not be read")

    monkeypatch.setattr(Path, "open", _must_not_read)
    monkeypatch.setattr(Path, "read_bytes", _must_not_read)
    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.metas_written == 0
    row = _subagent_row(store, owner, "a1")
    assert store.transcript_meta(row.id, owner.id) == original


def test_a_deleted_sidecar_leaves_the_stored_copy(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    write_subagent(tmp_path, "s1", "a1")
    sidecar = write_subagent_meta(tmp_path, "s1", "a1")
    original = sidecar.read_bytes()
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    sidecar.unlink()
    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.failures == []
    row = _subagent_row(store, owner, "a1")
    assert store.transcript_meta(row.id, owner.id) == original


@pytest.mark.parametrize(
    "content", [b'{"agentType":"impl', b"[1, 2]", b"\xff\xfe"], ids=str
)
def test_a_sidecar_that_is_not_a_json_object_is_a_failure_and_not_stored(
    store: PostgresStore, owner: Principal, tmp_path: Path, content: bytes
) -> None:
    """Never-overwrite makes a torn sidecar permanent if it is stored, so it
    is refused and tried again next run instead."""
    write_subagent(tmp_path, "s1", "a1")
    sidecar = write_subagent_meta(tmp_path, "s1", "a1", content)
    transcripts.designate(store, owner.id, "p", tmp_path)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.metas_written == 0
    assert [f["path"] for f in report.failures] == [str(sidecar)]
    assert _subagent_row(store, owner, "a1").has_meta is False

    write_subagent_meta(tmp_path, "s1", "a1")
    retried = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)
    assert retried.metas_written == 1
    assert retried.failures == []


def test_a_capped_run_still_backfills_sidecars_it_passes(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """Sidecars do not spend the cap: a backfill of a few hundred small
    files must not take a session start per 25 of them."""
    for agent in ("a1", "a2", "a3"):
        write_subagent(tmp_path, "s1", agent)
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)
    for agent in ("a1", "a2", "a3"):
        write_subagent_meta(tmp_path, "s1", agent)

    report = transcripts.run(
        store, owner.id, "p", trigger=TranscriptTrigger.AUTO, cap=1
    )

    assert report.metas_written == 3


def test_a_session_file_never_gets_a_sidecar(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    write_session(tmp_path, "s1")
    transcripts.designate(store, owner.id, "p", tmp_path)
    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)
    assert report.metas_written == 0
    assert (
        found(
            store.get_transcript(owner.id, transcripts.HARNESS, "s1", agent_id=None)
        ).has_meta
        is False
    )


def test_a_line_jsonb_refuses_does_not_stop_the_import(
    store: PostgresStore, owner: Principal, tmp_path: Path
) -> None:
    """One such line once aborted every run for its project, forever."""
    target = tmp_path / "s1.jsonl"
    target.write_bytes(b'{"type": "user"}\n{"type": "user", "text": "\\u0000"}\n')
    write_session(tmp_path, "s2")
    transcripts.designate(store, owner.id, "p", tmp_path)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_new == 2
    assert [f["path"] for f in report.failures] == [str(target)]
    stored = found(
        store.get_transcript(owner.id, transcripts.HARNESS, "s1", agent_id=None)
    )
    assert store.transcript_content(stored.id, owner.id) == target.read_bytes()
    assert store.transcript_line_count(stored.id) == 1


def test_a_failing_file_spends_the_refresh_budget(
    store: PostgresStore,
    owner: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It may have read megabytes before raising, and it raises every run -
    a free pass would let one bad file make every refresh unbounded."""
    for session_id in ("a-bad", "b-good"):
        _write(tmp_path, session_id, [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    real = store.put_transcript

    def _flaky(*args: Any, **kwargs: Any) -> Any:
        if args[3] == "a-bad":
            raise RuntimeError("cannot read")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "put_transcript", _flaky)

    report = transcripts.run(
        store, owner.id, "p", trigger=TranscriptTrigger.AUTO, cap=1
    )

    assert (report.files_seen, report.files_new) == (1, 0)
