from uuid import UUID

from remem.domain import (
    Collection,
    CollectionQuery,
    Entry,
    Kind,
    Origin,
    Principal,
    PrincipalKind,
    Query,
    Scope,
    new_id,
)


def test_new_id_is_a_uuid7_and_is_time_sortable():
    a, b = new_id(), new_id()
    assert isinstance(a, UUID)
    assert a.version == 7
    assert str(a) < str(b)


def test_enums_are_string_valued():
    assert Kind.RULE == "rule"
    assert Scope.PERSONAL == "personal"
    assert Origin.EXTRACTED == "extracted"
    assert PrincipalKind.TEAM == "team"


def test_entry_defaults():
    owner = new_id()
    e = Entry(id=new_id(), kind=Kind.NOTE, title="t", body="b", owner_id=owner)
    assert e.project is None
    assert e.scope is Scope.PERSONAL
    assert e.origin is Origin.AGENT
    assert e.tags == []
    assert e.links == []
    assert e.session_id is None
    assert e.superseded_by is None


def test_entry_mutable_defaults_are_not_shared():
    owner = new_id()
    a = Entry(id=new_id(), kind=Kind.DOC, title="a", body="b", owner_id=owner)
    b = Entry(id=new_id(), kind=Kind.DOC, title="c", body="d", owner_id=owner)
    a.tags.append("x")
    assert b.tags == []


def test_collection_defaults_to_an_empty_query():
    c = Collection(id=new_id(), slug="s", title="T", owner_id=new_id())
    assert c.query == CollectionQuery()
    assert c.query.tags == []
    assert c.query.kinds == []
    assert c.query.project is None


def test_query_defaults_exclude_superseded():
    q = Query()
    assert q.include_superseded is False
    assert q.text is None
    assert q.limit == 20


def test_principal_defaults_to_user_kind():
    p = Principal(id=new_id(), handle="brandon")
    assert p.kind is PrincipalKind.USER
