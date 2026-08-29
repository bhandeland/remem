"""Every frontend must be able to say HOW a hit matched.

A boolean could say "not exact". With three tiers the caller needs to know
which one answered: a semantic hit is a real conceptual match and worth
citing with care, while a trigram hit is a spelling accident that happened
to score. Collapsing them back into one "approximate" flag would throw away
the distinction the third tier exists to create.
"""

from remem.domain import Hit, Match


def _hit(**kw):
    from remem.domain import Entry, Kind, new_id
    entry = Entry(id=new_id(), kind=Kind.MEMORY, title="t", body="b",
                  owner_id=new_id())
    return Hit(entry=entry, rank=1.0, snippet="s", **kw)


def test_match_defaults_to_exact():
    assert _hit().match is Match.EXACT


def test_match_values_are_the_three_tiers():
    assert [m.value for m in Match] == ["exact", "semantic", "fuzzy"]


def test_match_is_a_string_enum():
    # Frontends serialise this straight into JSON, so it must render as the
    # bare word and not "Match.SEMANTIC".
    assert f"{Match.SEMANTIC}" == "semantic"


def test_hit_no_longer_has_a_fuzzy_boolean():
    # Deliberately no compatibility shim: a frontend still reading .fuzzy
    # would report every semantic hit as exact, silently.
    assert not hasattr(_hit(), "fuzzy")
