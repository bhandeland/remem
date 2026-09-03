from __future__ import annotations

from remem.services import memory
from remem.services.memory import Case

MARK = memory.Watermark(entry_id="e", body_sha="A", exported_at="t")


def test_a_stray_file_is_adopted_as_new():
    assert memory.classify(file_sha="A", entry_sha=None, mark=None) == (
        Case.ADOPT_NEW
    )


def test_file_and_entry_with_no_watermark_and_equal_bodies_heals():
    assert memory.classify(file_sha="A", entry_sha="A", mark=None) == Case.HEAL


def test_file_and_entry_with_no_watermark_and_different_bodies_conflicts():
    assert memory.classify(file_sha="A", entry_sha="B", mark=None) == (
        Case.CONFLICT
    )


def test_file_moved_alone_is_adopted_as_an_edit():
    assert memory.classify(file_sha="B", entry_sha="A", mark=MARK) == (
        Case.ADOPT_EDIT
    )


def test_entry_moved_alone_regenerates_the_file():
    assert memory.classify(file_sha="A", entry_sha="B", mark=MARK) == (
        Case.REGENERATE
    )


def test_both_moved_is_a_conflict():
    assert memory.classify(file_sha="B", entry_sha="C", mark=MARK) == (
        Case.CONFLICT
    )


def test_both_moved_to_the_same_content_is_not_a_conflict():
    # Claude and remem independently arriving at the same text is agreement,
    # not a conflict, and asking the user to resolve it would be noise.
    assert memory.classify(file_sha="B", entry_sha="B", mark=MARK) == Case.HEAL


def test_neither_moved_is_unchanged():
    assert memory.classify(file_sha="A", entry_sha="A", mark=MARK) == (
        Case.UNCHANGED
    )


def test_an_entry_with_no_file_is_regenerated():
    assert memory.classify(file_sha=None, entry_sha="A", mark=None) == (
        Case.REGENERATE
    )


def test_an_entry_gone_from_the_collection_deletes_its_file():
    assert memory.classify(file_sha="A", entry_sha=None, mark=MARK) == (
        Case.DELETE
    )


def test_a_file_that_moved_since_export_is_never_deleted():
    # The gate: remem removes only what it wrote and knows to be untouched.
    assert memory.classify(file_sha="B", entry_sha=None, mark=MARK) == (
        Case.ADOPT_EDIT
    )


def test_watermarks_round_trip(tmp_path):
    marks = {"a": MARK}
    memory.save_watermarks(tmp_path, marks)
    assert memory.load_watermarks(tmp_path) == marks


def test_a_missing_watermark_file_is_an_empty_mapping(tmp_path):
    assert memory.load_watermarks(tmp_path) == {}


def test_a_corrupt_watermark_file_is_an_empty_mapping(tmp_path):
    (tmp_path / memory.WATERMARK_NAME).write_text("{not json")
    assert memory.load_watermarks(tmp_path) == {}
