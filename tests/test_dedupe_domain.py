"""The dedupe report's shapes. Pure data - no database here."""

from __future__ import annotations

from remem.domain import DedupeReport, DuplicateSet, Entry, Kind, NearPair, new_id


def _entry(title: str, body: str = "b") -> Entry:
    return Entry(id=new_id(), kind=Kind.NOTE, title=title, body=body,
                 owner_id=new_id())


def test_a_duplicate_set_holds_its_entries():
    a, b = _entry("one"), _entry("two")
    assert DuplicateSet(entries=[a, b]).entries == [a, b]


def test_a_near_pair_carries_its_similarity():
    pair = NearPair(a=_entry("one"), b=_entry("two"), similarity=0.97)
    assert pair.similarity == 0.97


def test_a_report_distinguishes_shown_pairs_from_the_total():
    """near_total is what lets the renderer say 'showing 1 of 9'."""
    report = DedupeReport(
        exact=[], near=[NearPair(_entry("a"), _entry("b"), 0.99)],
        near_total=9, threshold=0.95, model="m", embedded=3, total=10,
    )
    assert len(report.near) == 1
    assert report.near_total == 9
    assert report.embedded < report.total
