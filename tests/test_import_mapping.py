"""What an imported record becomes. Pure - no store, no database."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from remem.domain import Kind
from remem.importers.base import SourceKind, SourceRecord
from remem.services.import_ import plan

# Built as a real SourceRecord and varied with `replace`, not assembled in a
# dict and splatted in. A `dict(...)` of heterogeneous values widens to
# `dict[str, SourceKind | str | tuple[str]]`, so `**base` offers that union to
# every parameter and the call stops being checkable at all - which is exactly
# the signature-drift a test helper should be the first thing to catch.
_BASE = SourceRecord(
    source_id="m1",
    kind=SourceKind.OBSERVATION,
    project="at-workspace",
    title="A title",
    summary="A hook",
    body="A body",
    tags=("cmem-type:discovery",),
)


def _record(**kw: Any) -> SourceRecord:
    return replace(_BASE, **kw)


def test_an_observation_becomes_a_note():
    [planned] = plan([_record()], namespace="cmem")

    assert planned.kind is Kind.NOTE


def test_a_summary_becomes_a_doc_not_a_handoff():
    """origin=handoff would supersede every prior summary for the same
    project and hide the rest from search. As docs they keep their own
    identity and stay findable."""
    [planned] = plan([_record(kind=SourceKind.SUMMARY)], namespace="cmem")

    assert planned.kind is Kind.DOC


def test_every_record_carries_its_identity_tag():
    """The tag is what makes a second import skip instead of duplicate."""
    [planned] = plan([_record()], namespace="cmem")

    assert "cmem:m1" in planned.tags


def test_source_tags_are_kept_alongside_the_identity_tag():
    [planned] = plan([_record()], namespace="cmem")

    assert "cmem-type:discovery" in planned.tags


def test_the_project_override_wins_over_the_source_project():
    [planned] = plan([_record()], namespace="cmem", project="accutrade")

    assert planned.project == "accutrade"


def test_without_an_override_the_source_project_is_used_verbatim():
    [planned] = plan([_record()], namespace="cmem")

    assert planned.project == "at-workspace"


def test_nothing_imported_is_ever_a_rule():
    """write.remember raises RuleNeedsSummary for a rule with no summary,
    and no imported record is a convention this project agreed to follow."""
    kinds = {plan([_record(kind=k)], namespace="cmem")[0].kind for k in SourceKind}

    assert Kind.RULE not in kinds
