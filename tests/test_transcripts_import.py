from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import Principal, TranscriptTrigger
from saddlebag.services import transcripts

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


def _write(directory: Path, session_id: str, lines: list[dict]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{session_id}.jsonl"
    target.write_bytes(b"".join(json.dumps(line).encode() + b"\n" for line in lines))
    return target


def test_import_stores_content_and_lines(store, owner, tmp_path: Path) -> None:
    _write(tmp_path, "s1", [{"type": "user"}, {"type": "assistant"}])
    transcripts.designate(store, owner.id, "p", tmp_path)

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_new == 1
    assert report.lines_written == 2
    stored = store.get_transcript(owner.id, transcripts.HARNESS, "s1")
    assert store.transcript_line_count(stored.id) == 2


def test_a_second_run_over_an_unchanged_file_reads_nothing(
    store, owner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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


def test_an_appended_file_adds_only_the_new_lines(store, owner, tmp_path: Path) -> None:
    target = _write(tmp_path, "s1", [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    with target.open("ab") as fh:
        fh.write(json.dumps({"type": "assistant"}).encode() + b"\n")

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_appended == 1
    assert report.lines_written == 1
    stored = store.get_transcript(owner.id, transcripts.HARNESS, "s1")
    assert store.transcript_line_count(stored.id) == 2


def test_append_keeps_seq_aligned_with_the_file_across_a_bad_line(
    store, owner, tmp_path: Path
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

    stored = store.get_transcript(owner.id, transcripts.HARNESS, "s1")
    assert store.transcript_line_count(stored.id) == 2
    # The appended line is the file's THIRD line, seq 2 - not seq 1, which is
    # what a row-count-derived start would have produced.
    with store._cur() as cur:  # noqa: SLF001 - asserting stored seqs directly
        cur.execute(
            "select seq from transcript_lines where transcript_id = %s order by seq",
            (stored.id,),
        )
        assert [r["seq"] for r in cur.fetchall()] == [0, 2]


def test_a_rewritten_file_rebuilds_its_lines(store, owner, tmp_path: Path) -> None:
    _write(tmp_path, "s1", [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    # Same session, different history, longer than before.
    _write(tmp_path, "s1", [{"type": "system"}, {"type": "assistant"}])

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert report.files_rebuilt == 1
    stored = store.get_transcript(owner.id, transcripts.HARNESS, "s1")
    assert store.transcript_line_count(stored.id) == 2


def test_a_shrunk_file_is_an_anomaly_and_is_not_followed(
    store, owner, tmp_path: Path
) -> None:
    """The stored copy is more complete. Two sessions are already gone from
    disk, and this is the rule that exists for exactly that."""
    _write(tmp_path, "s1", [{"type": "user"}, {"type": "assistant"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)
    before = store.get_transcript(owner.id, transcripts.HARNESS, "s1")

    _write(tmp_path, "s1", [{"type": "user"}])

    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    assert len(report.anomalies) == 1
    assert report.anomalies[0]["stored"] == before.bytes
    after = store.get_transcript(owner.id, transcripts.HARNESS, "s1")
    assert after.bytes == before.bytes


def test_an_unparseable_line_is_named_not_dropped(store, owner, tmp_path: Path) -> None:
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
    store, owner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

    def _counting_stat(self: Path, *args: object, **kwargs: object) -> object:
        nonlocal stat_calls
        if self.suffix == ".jsonl":
            stat_calls += 1
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _counting_stat)

    report = transcripts.run(
        store, owner.id, "p", trigger=TranscriptTrigger.AUTO, cap=2
    )

    assert report.files_new == 2
    assert report.files_seen == 2
    assert stat_calls == 2


def test_an_unclaimed_project_does_nothing(store, owner, tmp_path: Path) -> None:
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
    store, owner, tmp_path: Path
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
    store, owner, tmp_path: Path
) -> None:
    transcripts.designate(store, owner.id, "p", tmp_path)
    tmp_path.rmdir()
    report = transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)
    assert len(report.failures) == 1


def test_a_body_exception_records_the_failure_and_finishes_the_row(
    store, owner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run()`'s wrapper records a partial run and re-raises.

    A body that dies partway still leaves a finished row carrying the
    `{"path": "*"}` failure - this proves that much, with a stub that raises
    a plain `RuntimeError` before any DB write happens.

    What this does NOT cover: a real psycopg error leaves the connection in
    a failed transaction, and there the `finish_transcript_run` call in the
    `finally` would itself raise `InFailedSqlTransaction`, silently
    replacing the original exception and recording nothing. A bare Python
    exception never poisons a connection, so this test cannot exercise that
    failure mode - and the per-test `conn` fixture, already inside a
    transaction that gets rolled back at test end, cannot be made to either.
    Guarding against it is the caller's job: `run()`'s docstring states the
    `autocommit=True` requirement that makes the finish succeed even after a
    failed statement, and that contract is established end to end by the
    CLI's own test, not by this one.
    """
    _write(tmp_path, "s1", [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated failure")

    # get_transcript is called from inside _import_one, which _run_body only
    # reaches once a claimed directory exists with a file to look at - unlike
    # transcript_paths, which run() now consults once up front (to decide
    # whether to write a row at all) and never calls again.
    monkeypatch.setattr(store, "get_transcript", _boom)

    with pytest.raises(RuntimeError, match="simulated failure"):
        transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.MANUAL)

    run = store.latest_transcript_run(owner.id, "p")
    assert run.finished_at is not None
    assert any(f.get("path") == "*" for f in run.failures)


def test_the_run_is_recorded_with_its_trigger(store, owner, tmp_path: Path) -> None:
    _write(tmp_path, "s1", [{"type": "user"}])
    transcripts.designate(store, owner.id, "p", tmp_path)
    transcripts.run(store, owner.id, "p", trigger=TranscriptTrigger.AUTO)
    run = store.latest_transcript_run(owner.id, "p")
    assert run.trigger is TranscriptTrigger.AUTO
    assert run.finished_at is not None
