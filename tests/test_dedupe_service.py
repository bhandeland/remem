"""Ranking, suppression and rendering. Pure - no store, no database."""

from __future__ import annotations

from datetime import datetime, timezone

from remem.domain import (
    DedupeReport, DuplicateSet, Entry, Kind, NearPair, Origin, new_id,
)
from remem.services.dedupe import render, suppress, survivor

OWNER = new_id()


def _entry(title, origin=Origin.AGENT, updated="2026-01-01", eid=None):
    return Entry(
        id=eid or new_id(), kind=Kind.NOTE, title=title, body="b",
        owner_id=OWNER, origin=origin,
        updated_at=datetime.fromisoformat(updated).replace(tzinfo=timezone.utc),
    )


def test_a_human_entry_outranks_a_newer_extracted_one():
    human = _entry("written", Origin.HUMAN, "2026-01-01")
    machine = _entry("extracted", Origin.EXTRACTED, "2026-06-01")
    assert survivor([machine, human]).id == human.id


def test_recency_breaks_a_tie_within_one_origin():
    old = _entry("old", Origin.HUMAN, "2026-01-01")
    new = _entry("new", Origin.HUMAN, "2026-06-01")
    assert survivor([old, new]).id == new.id


def test_the_id_breaks_a_full_tie_so_output_is_stable():
    a = _entry("a", Origin.HUMAN, "2026-01-01")
    b = _entry("b", Origin.HUMAN, "2026-01-01")
    expected = min(a.id, b.id, key=str)
    assert survivor([a, b]).id == expected
    assert survivor([b, a]).id == expected


def test_a_pair_inside_an_exact_group_is_suppressed():
    a, b = _entry("a"), _entry("b")
    pairs = suppress([NearPair(a, b, 0.99)], [DuplicateSet([a, b])])
    assert pairs == []


def test_a_pair_with_only_one_member_in_a_group_survives():
    a, b, c = _entry("a"), _entry("b"), _entry("c")
    pairs = suppress([NearPair(a, c, 0.97)], [DuplicateSet([a, b])])
    assert len(pairs) == 1


def _report(**kw):
    base = dict(exact=[], near=[], near_total=0, threshold=0.95,
                model="m", embedded=0, total=0)
    return DedupeReport(**{**base, **kw})


def test_no_vectors_renders_as_not_checked_never_as_clean():
    out = render(_report(embedded=0, total=5))
    assert "not checked" in out
    assert "remem embed" in out
    assert "No near-duplicates" not in out


def test_partial_coverage_names_what_was_not_compared():
    out = render(_report(embedded=3, total=5))
    assert "3 of 5" in out
    assert "2 entries were not compared" in out


def test_full_coverage_says_none_found_rather_than_not_checked():
    out = render(_report(embedded=5, total=5))
    assert "not checked" not in out
    assert "No near-duplicates" in out


def test_truncation_is_visible_only_when_it_happened():
    a, b = _entry("a"), _entry("b")
    shown = _report(near=[NearPair(a, b, 0.97)], near_total=9,
                    embedded=5, total=5)
    assert "showing 1 of 9" in render(shown)
    whole = _report(near=[NearPair(a, b, 0.97)], near_total=1,
                    embedded=5, total=5)
    assert "showing" not in render(whole)


def test_every_group_prints_a_runnable_resolve_line():
    keep = _entry("keep", Origin.HUMAN)
    drop = _entry("drop", Origin.EXTRACTED)
    out = render(_report(exact=[DuplicateSet([drop, keep])]))
    assert f"remem dedupe resolve {drop.id} --keep {keep.id}" in out
