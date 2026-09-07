import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Hit, Query
from remem.embed import EmbedderUnavailable
from remem.services.search import find
from remem.services.write import remember

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


def test_find_delegates_to_the_store(store, owner):
    remember(store, owner.id, title="Postgres", body="tune work_mem")
    hits = find(store, owner.id, Query(text="work_mem"))
    assert [h.entry.title for h in hits] == ["Postgres"]


def test_find_caps_an_absurd_limit(store, owner):
    for i in range(3):
        remember(store, owner.id, title=f"E{i}", body="shared")
    hits = find(store, owner.id, Query(text="shared", limit=100000))
    assert len(hits) == 3


def test_find_rejects_a_nonpositive_limit(store, owner):
    remember(store, owner.id, title="E", body="shared")
    assert find(store, owner.id, Query(text="shared", limit=0)) == []


# Three tiers, tried in order, never blended.

from remem.domain import Entry, Kind, Match, new_id


class StubStore:
    """Records which tiers were called, and answers with what it was told to.

    A stub rather than a database, because the question here is tier
    ORDERING - a policy decision that lives in the service - and a real store
    would make the test about SQL instead.
    """

    def __init__(self, exact=None, semantic=None, fuzzy=None):
        self._exact = exact or []
        self._semantic = semantic or []
        self._fuzzy = fuzzy or []
        self.called: list[str] = []

    def search(self, query, owner_id):
        self.called.append("exact")
        return list(self._exact)

    def semantic_search(self, query, owner_id, vector, model, threshold):
        self.called.append("semantic")
        return list(self._semantic)

    def fuzzy_search(self, query, owner_id, threshold):
        self.called.append("fuzzy")
        return list(self._fuzzy)


class StubEmbedder:
    name = "stub"
    dim = 2

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


def _hit(match=Match.EXACT):
    e = Entry(id=new_id(), kind=Kind.NOTE, title="t", body="b", owner_id=new_id())
    return Hit(entry=e, rank=1.0, snippet="s", match=match)


def test_exact_results_stop_the_chain():
    store = StubStore(exact=[_hit()], semantic=[_hit(Match.SEMANTIC)])

    hits = find(store, new_id(), Query(text="q"), embedder=StubEmbedder())

    assert store.called == ["exact"]
    assert hits[0].match is Match.EXACT


def test_semantic_runs_only_when_exact_is_empty():
    store = StubStore(semantic=[_hit(Match.SEMANTIC)], fuzzy=[_hit(Match.FUZZY)])

    hits = find(store, new_id(), Query(text="q"), embedder=StubEmbedder())

    assert store.called == ["exact", "semantic"]
    assert [h.match for h in hits] == [Match.SEMANTIC]


def test_fuzzy_runs_only_when_both_above_are_empty():
    store = StubStore(fuzzy=[_hit(Match.FUZZY)])

    hits = find(store, new_id(), Query(text="q"), embedder=StubEmbedder())

    assert store.called == ["exact", "semantic", "fuzzy"]
    assert [h.match for h in hits] == [Match.FUZZY]


def test_no_embedder_degrades_to_two_tiers():
    # The documented degradation: without the optional dependency installed,
    # search still works and simply skips the middle tier. It must not error.
    store = StubStore(fuzzy=[_hit(Match.FUZZY)])

    hits = find(store, new_id(), Query(text="q"), embedder=None)

    assert store.called == ["exact", "fuzzy"]
    assert [h.match for h in hits] == [Match.FUZZY]


def test_an_embedder_that_raises_degrades_rather_than_failing_the_search():
    # A missing model file must cost the semantic tier, not the search. The
    # user asked a question; two tiers can still answer it. An embedder that
    # raises never reaches store.semantic_search, so "semantic" never lands
    # in store.called - only ["exact", "fuzzy"] is a correct outcome here.
    class Broken(StubEmbedder):
        def embed(self, texts):
            raise RuntimeError("no model")

    store = StubStore(fuzzy=[_hit(Match.FUZZY)])
    hits = find(store, new_id(), Query(text="q"), embedder=Broken())

    assert store.called == ["exact", "fuzzy"]
    assert [h.match for h in hits] == [Match.FUZZY]


def test_empty_query_text_skips_both_fallbacks():
    # A listing query - no text, just filters. There is nothing to be
    # approximately like.
    store = StubStore(semantic=[_hit(Match.SEMANTIC)], fuzzy=[_hit(Match.FUZZY)])

    hits = find(store, new_id(), Query(text="  "), embedder=StubEmbedder())

    assert store.called == ["exact"]
    assert hits == []


def test_an_exact_match_never_constructs_an_embedder(monkeypatch):
    # The cost of building the local embedder is a model download on a cold
    # machine and an ONNX session on a warm one, and an exact match needs
    # neither. Blowing up in load_embedder is the only way to assert that it
    # was not called anywhere down the chain.
    def explode(model_name):
        raise AssertionError(f"built an embedder for {model_name!r}")

    monkeypatch.setattr("remem.services.search.load_embedder", explode)
    monkeypatch.setattr("remem.services.search._EMBEDDERS", {})
    store = StubStore(exact=[_hit()])

    hits = find(store, new_id(), Query(text="q"))

    assert [h.match for h in hits] == [Match.EXACT]


def test_the_semantic_tier_builds_the_shared_embedder_once(monkeypatch):
    calls = []

    def build(model_name):
        calls.append(model_name)
        return StubEmbedder()

    monkeypatch.setattr("remem.services.search.load_embedder", build)
    monkeypatch.setattr("remem.services.search._EMBEDDERS", {})
    store = StubStore(semantic=[_hit(Match.SEMANTIC)])

    for _ in range(3):
        find(store, new_id(), Query(text="q"), embed_model="m")

    assert calls == ["m"]


def test_a_raising_embedder_constructor_costs_the_tier_and_not_the_search(
    store, owner, monkeypatch
):
    """The probe is a real inference call, and it is outside the old try.

    shared_embedder caught EmbedderUnavailable only. A LocalEmbedder that
    imports fine and then fails while measuring its own dimension raises
    something else, which escaped the tier and crashed the search - the
    exact opposite of what _semantic's docstring promises.
    """
    monkeypatch.setattr("remem.services.search._EMBEDDERS", {})

    def explode(name):
        raise RuntimeError("onnxruntime session failed")

    monkeypatch.setattr("remem.services.search.load_embedder", explode)

    remember(store, owner.id, title="config command", body="body")
    hits = find(store, owner.id, Query(text="zzzz nothing", limit=5))
    assert hits == []  # degraded to two tiers, did not raise


def test_an_unavailable_embedder_is_a_none_and_is_not_retried(monkeypatch):
    # The policy the frontends used to each restate: search degrades, and it
    # degrades once. Re-attempting the fastembed import on every search would
    # repay the failure without ever changing the answer.
    calls = []

    def unavailable(model_name):
        calls.append(model_name)
        raise EmbedderUnavailable("no extra")

    monkeypatch.setattr("remem.services.search.load_embedder", unavailable)
    monkeypatch.setattr("remem.services.search._EMBEDDERS", {})
    store = StubStore(fuzzy=[_hit(Match.FUZZY)])

    for _ in range(3):
        hits = find(store, new_id(), Query(text="q"), embed_model="m")

    assert calls == ["m"]
    assert [h.match for h in hits] == [Match.FUZZY]
