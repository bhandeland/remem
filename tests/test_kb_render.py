import pytest

from remem.domain import Collection, Entry, Kind, new_id
from remem.services.kb import RulesExceedBudget, render

OWNER = new_id()


def collection(**kw):
    return Collection(id=new_id(), slug=kw.pop("slug", "s"),
                      title=kw.pop("title", "My KB"), owner_id=OWNER, **kw)


def entry(title, body, kind=Kind.MEMORY, tags=None):
    return Entry(id=new_id(), kind=kind, title=title, body=body,
                 owner_id=OWNER, tags=tags or [])


def test_renders_the_collection_title_and_description():
    out = render(collection(description="what matters"), [], max_chars=1000)
    assert "My KB" in out
    assert "what matters" in out


def test_rules_render_before_other_entries():
    entries = [
        entry("A memory", "body one"),
        entry("A rule", "body two", kind=Kind.RULE),
    ]
    out = render(collection(), entries, max_chars=5000)
    assert out.index("A rule") < out.index("A memory")


def test_entry_metadata_line_carries_id_and_tags():
    e = entry("Titled", "body", tags=["sql", "style"])
    out = render(collection(), [e], max_chars=5000)
    assert str(e.id) in out
    assert "sql" in out and "style" in out


def test_budget_drops_whole_entries_not_partial_ones():
    entries = [entry(f"Entry {i}", "x" * 200) for i in range(20)]
    out = render(collection(), entries, max_chars=800)
    assert "x" * 200 in out          # any included entry is complete
    assert len(out) <= 1200          # budget plus the notice line


def test_omitted_entries_are_announced_with_a_count():
    entries = [entry(f"Entry {i}", "x" * 200) for i in range(20)]
    out = render(collection(slug="my-kb"), entries, max_chars=600)
    assert "more entries not shown" in out
    assert "remem kb show my-kb --full" in out


def test_no_notice_when_everything_fits():
    out = render(collection(), [entry("Small", "tiny")], max_chars=5000)
    assert "not shown" not in out


def test_rules_are_never_truncated_even_over_budget():
    """Rules survive whole while everything else is dropped for space."""
    rules = [entry(f"Rule {i}", "y" * 300, kind=Kind.RULE) for i in range(4)]
    others = [entry(f"Doc {i}", "z" * 800) for i in range(10)]

    out = render(collection(), rules + others, max_chars=2000)

    for i in range(4):
        assert f"Rule {i}" in out
    assert out.count("y" * 300) == 4     # every rule body, in full
    assert "z" * 800 not in out          # the budget really did bite
    assert "10 more entries not shown" in out


def test_rules_alone_exceeding_the_budget_raises():
    rules = [entry(f"Rule {i}", "y" * 500, kind=Kind.RULE) for i in range(10)]
    with pytest.raises(RulesExceedBudget):
        render(collection(), rules, max_chars=500)


def test_empty_collection_renders_without_crashing():
    out = render(collection(), [], max_chars=1000)
    assert isinstance(out, str)
    assert "My KB" in out


def test_total_output_never_exceeds_the_budget():
    """The omitted-count notice is part of the block, so it must be budgeted.

    Previously the notice was appended after the packing loop, so the returned
    string could exceed max_chars by the notice's own length.
    """
    entries = [entry(f"Entry {i}", "x" * 200) for i in range(20)]
    for budget in (400, 600, 800, 1500):
        out = render(collection(slug="my-kb"), entries, max_chars=budget)
        assert len(out) <= budget, (budget, len(out))


def test_notice_still_appears_when_it_forces_dropping_another_entry():
    entries = [entry(f"Entry {i}", "x" * 200) for i in range(20)]
    out = render(collection(slug="my-kb"), entries, max_chars=600)
    assert "more entries not shown" in out
    assert len(out) <= 600
