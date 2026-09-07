import json

from remem import session_size


def _transcript(tmp_path, lines):
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return p


def test_counts_user_turns_only(tmp_path):
    p = _transcript(
        tmp_path,
        [
            '{"parentUuid":null,"type":"user","message":{"role":"user"}}',
            '{"type":"assistant","message":{"role":"assistant"}}',
            '{"type": "user", "message": {"role": "user"}}',
        ],
    )
    assert session_size.count_turns(p) == 2


def test_malformed_lines_do_not_stop_the_count(tmp_path):
    p = _transcript(
        tmp_path,
        [
            '{"type":"user"}',
            "not json at all",
            "",
            '{"type":"user"}',
        ],
    )
    assert session_size.count_turns(p) == 2


def test_a_missing_transcript_counts_zero(tmp_path):
    assert session_size.count_turns(tmp_path / "nope.jsonl") == 0


def test_warns_first_at_the_threshold():
    assert not session_size.should_warn(149, 0, 150, 50)
    assert session_size.should_warn(150, 0, 150, 50)


def test_does_not_repeat_until_a_full_step_has_passed():
    assert not session_size.should_warn(151, 150, 150, 50)
    assert not session_size.should_warn(199, 150, 150, 50)
    assert session_size.should_warn(200, 150, 150, 50)


def test_a_stride_larger_than_one_still_warns():
    """The count does not always advance by exactly one per prompt, so a
    `count % every == 0` test would skip the remainder and never fire again."""
    assert session_size.should_warn(157, 0, 150, 50)
    assert session_size.should_warn(213, 157, 150, 50)


def test_warn_state_round_trips(tmp_path):
    path = tmp_path / "session-size.json"
    assert session_size.read_last_warned("sess-1", path) == 0
    session_size.record_warned("sess-1", 150, path)
    assert session_size.read_last_warned("sess-1", path) == 150
    assert session_size.read_last_warned("sess-2", path) == 0


def test_a_corrupt_state_file_reads_as_never_warned(tmp_path):
    path = tmp_path / "session-size.json"
    path.write_text("{not json")
    assert session_size.read_last_warned("sess-1", path) == 0


def test_old_sessions_are_pruned_on_write(tmp_path):
    path = tmp_path / "session-size.json"
    stale = 1_000_000.0
    session_size.record_warned("old", 150, path, now=stale)
    session_size.record_warned(
        "new", 150, path, now=stale + session_size.STATE_TTL_SECONDS + 1
    )
    data = json.loads(path.read_text())
    assert list(data["sessions"]) == ["new"]


def test_the_reminder_names_the_count_and_the_skill():
    text = session_size.reminder(150)
    assert "150" in text
    assert "remem-handoff" in text
