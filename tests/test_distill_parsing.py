import pytest

from remem.distill.base import (
    MAX_BODY,
    MAX_ENTRIES,
    MAX_TITLE,
    CapturedEntry,
    DistillationFailed,
    parse_entries,
)
from remem.domain import Kind


def test_parses_a_well_formed_array():
    raw = """[
      {"title": "Pool sizing", "body": "pgbouncer saturates",
       "kind": "note", "tags": ["ops"]}
    ]"""
    entries = parse_entries(raw)
    assert entries == [
        CapturedEntry(title="Pool sizing", body="pgbouncer saturates",
                      kind=Kind.NOTE, tags=["ops"])
    ]


def test_an_empty_array_is_valid_and_yields_nothing():
    """Most sessions contain nothing durable. That is success, not failure."""
    assert parse_entries("[]") == []


def test_tolerates_prose_around_the_json():
    """Models prepend explanations however firmly you ask them not to."""
    raw = 'Here is what I found:\\n[{"title":"T","body":"B","kind":"rule"}]\\nDone.'
    assert [e.title for e in parse_entries(raw)] == ["T"]


def test_tolerates_a_fenced_code_block():
    raw = '```json\\n[{"title":"T","body":"B","kind":"doc"}]\\n```'
    assert [e.kind for e in parse_entries(raw)] == [Kind.DOC]


def test_missing_tags_defaults_to_empty():
    assert parse_entries('[{"title":"T","body":"B","kind":"note"}]')[0].tags == []


def test_drops_entries_missing_required_fields_without_failing_the_batch():
    raw = """[
      {"title": "Good", "body": "B", "kind": "note"},
      {"title": "No body", "kind": "note"},
      {"body": "No title", "kind": "note"}
    ]"""
    assert [e.title for e in parse_entries(raw)] == ["Good"]


def test_drops_entries_with_an_unknown_kind():
    raw = """[
      {"title": "Good", "body": "B", "kind": "note"},
      {"title": "Bad", "body": "B", "kind": "reminder"}
    ]"""
    assert [e.title for e in parse_entries(raw)] == ["Good"]


def test_truncates_to_the_entry_cap():
    raw = "[" + ",".join(
        f'{{"title":"T{i}","body":"B","kind":"note"}}' for i in range(20)
    ) + "]"
    assert len(parse_entries(raw)) == MAX_ENTRIES


def test_caps_title_and_body_length():
    raw = (
        '[{"title":"' + "t" * 500 + '","body":"' + "b" * 9000
        + '","kind":"note"}]'
    )
    entry = parse_entries(raw)[0]
    assert len(entry.title) == MAX_TITLE
    assert len(entry.body) == MAX_BODY


def test_non_string_tags_are_dropped():
    raw = '[{"title":"T","body":"B","kind":"note","tags":["ok",5,null]}]'
    assert parse_entries(raw)[0].tags == ["ok"]


@pytest.mark.parametrize("raw", ["", "   ", "not json at all", "{}", "null", "[1,2,3]"])
def test_unparseable_output_raises_distillation_failed(raw):
    with pytest.raises(DistillationFailed):
        parse_entries(raw)


def test_prefers_the_array_that_actually_contains_entries():
    """A model second-guessing itself emits a decoy array before the real one."""
    raw = ('First attempt: [1,2,3] was wrong. Correct output: '
           '[{"title":"Good","body":"B","kind":"note"}]')
    assert [e.title for e in parse_entries(raw)] == ["Good"]


def test_tolerates_a_bracket_in_prose_after_a_fenced_block():
    raw = ('```json\n[{"title":"Good","body":"B","kind":"note"}]\n```\n'
           'Note: see items[0] for details.')
    assert [e.title for e in parse_entries(raw)] == ["Good"]


def test_an_empty_array_still_wins_over_no_valid_entries():
    """[] is the deliberate 'nothing durable' answer and stays a success."""
    assert parse_entries('Nothing to record: []') == []


def test_whitespace_only_title_or_body_is_dropped():
    """A non-empty array where every item fails validation is a distillation
    failure (not a quiet empty result) - see parse_entries for the rationale."""
    raw = '[{"title":"  ","body":"B","kind":"note"}]'
    with pytest.raises(DistillationFailed):
        parse_entries(raw)


def test_failure_carries_the_raw_output_for_diagnosis():
    """The user cannot fix a misbehaving prompt they cannot see."""
    with pytest.raises(DistillationFailed) as exc:
        parse_entries("I'm sorry, I can't help with that.")
    assert exc.value.raw == "I'm sorry, I can't help with that."


def test_failure_on_a_valueless_array_carries_the_raw_output():
    with pytest.raises(DistillationFailed) as exc:
        parse_entries("[1, 2, 3]")
    assert exc.value.raw == "[1, 2, 3]"
