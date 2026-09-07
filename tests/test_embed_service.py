"""The embed backlog policy.

Uses a fake embedder throughout. What is being tested is batching,
idempotency and what happens when the model chokes on one batch - none of
which needs real embeddings, all of which needs to be exercised.
"""

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Entry, Kind, new_id
from remem.embed import EmbedderUnavailable
from remem.services import embed
from remem.services.embed import backfill

pytestmark = pytest.mark.db


class FakeEmbedder:
    name = "fake-2"
    dim = 2

    def __init__(self):
        self.batches: list[list[str]] = []

    def embed(self, texts):
        self.batches.append(list(texts))
        return [[float(len(t)), 1.0] for t in texts]


class BrokenEmbedder(FakeEmbedder):
    def embed(self, texts):
        raise RuntimeError("model exploded")


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


def _entries(store, owner_id, n):
    return [
        store.put_entry(
            Entry(
                id=new_id(),
                kind=Kind.NOTE,
                title=f"t{i}",
                body=f"body {i}",
                owner_id=owner_id,
            )
        )
        for i in range(n)
    ]


def test_embeds_every_entry_that_needs_it(store):
    owner = store.ensure_principal("embed-all")
    _entries(store, owner.id, 3)

    result = backfill(store, owner.id, FakeEmbedder())

    assert result.embedded == 3
    assert result.failed == 0
    assert store.entries_missing_vectors(owner.id, "fake-2", limit=10) == []


def test_is_idempotent(store):
    # Safe to re-run is the whole reason this is a cron command rather than a
    # one-time script.
    owner = store.ensure_principal("embed-twice")
    _entries(store, owner.id, 2)

    backfill(store, owner.id, FakeEmbedder())
    second = backfill(store, owner.id, FakeEmbedder())

    assert second.embedded == 0


def test_batches_rather_than_one_call_per_entry(store):
    owner = store.ensure_principal("embed-batch")
    _entries(store, owner.id, 5)
    embedder = FakeEmbedder()

    backfill(store, owner.id, embedder, batch_size=2)

    assert [len(b) for b in embedder.batches] == [2, 2, 1]


def test_embeds_title_and_body_together(store):
    # Titles carry most of the signal in a short entry, and a body-only
    # embedding makes "config command" fail to match an entry titled exactly
    # that. Both, joined, or the tier misses its most obvious cases.
    owner = store.ensure_principal("embed-text")
    store.put_entry(
        Entry(
            id=new_id(),
            kind=Kind.NOTE,
            title="the title",
            body="the body",
            owner_id=owner.id,
        )
    )
    embedder = FakeEmbedder()

    backfill(store, owner.id, embedder)

    assert "the title" in embedder.batches[0][0]
    assert "the body" in embedder.batches[0][0]


def test_a_failing_batch_is_counted_not_raised(store):
    # A backlog of hundreds must not be abandoned because one batch failed,
    # and the count is what makes the failure visible in a cron log.
    owner = store.ensure_principal("embed-fail")
    _entries(store, owner.id, 2)

    result = backfill(store, owner.id, BrokenEmbedder())

    assert result.embedded == 0
    assert result.failed == 2


def test_max_entries_bounds_the_run(store):
    owner = store.ensure_principal("embed-bounded")
    _entries(store, owner.id, 5)

    result = backfill(store, owner.id, FakeEmbedder(), batch_size=2, max_entries=3)

    assert result.embedded == 3


# --- cron safety ------------------------------------------------------------


def test_a_second_embed_run_does_nothing_while_the_lock_is_held(
    live_dsn, monkeypatch, tmp_path
):
    """`remem embed` already claims idempotently, but two overlapping runs
    embed the same backlog twice and pay for it twice. Silence and exit 0,
    for the same reason `events process` does."""
    import psycopg
    from typer.testing import CliRunner

    from remem.cli import app

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        store = PostgresStore(c)
        owner = store.ensure_principal("brandon")
        _entries(store, owner.id, 2)
        c.commit()

    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))
    embedder = FakeEmbedder()
    monkeypatch.setattr("remem.cli.load_embedder", lambda model: embedder)

    holder = psycopg.connect(live_dsn)
    try:
        assert (
            holder.execute(
                "select pg_try_advisory_lock(hashtext('embed'), hashtext(%s))",
                (str(owner.id),),
            ).fetchone()[0]
            is True
        )

        result = CliRunner().invoke(app, ["embed"])

        assert result.exit_code == 0
        assert result.stdout == ""
        assert embedder.batches == []
    finally:
        holder.close()


# --- constructing the embedder is itself expensive ---------------------
# `backfill` takes an Embedder already built, which is right for `remem
# embed` - a user asked for it. The spawned refresh runs on every session
# start, where building a LocalEmbedder means importing fastembed, building
# an ONNX session and possibly a ~130MB download, for a backlog that is
# usually empty. Same policy `services.search.shared_embedder` already
# applies inside the semantic tier: find out whether there is work first.
def test_backfill_if_pending_does_not_build_an_embedder_for_an_empty_backlog(
    store,
):
    owner = store.ensure_principal("lazy-empty")
    built = []

    def load():
        built.append(1)
        return FakeEmbedder()

    result = embed.backfill_if_pending(store, owner.id, "fake-2", load)

    assert built == []
    assert result is None


def test_backfill_if_pending_builds_the_embedder_when_work_is_waiting(store):
    owner = store.ensure_principal("lazy-work")
    _entries(store, owner.id, 3)
    built = []

    def load():
        built.append(1)
        return FakeEmbedder()

    result = embed.backfill_if_pending(store, owner.id, "fake-2", load)

    assert built == [1]
    assert result.embedded == 3


def test_backfill_if_pending_reports_an_unavailable_embedder_as_a_failure(
    store,
):
    """Fail-soft is the caller's job, not this function's.

    The spawned refresh swallows it; `remem embed` stays loud. Neither can
    decide that if this silently returned None for both "nothing to do" and
    "no embedder", since those want opposite responses.
    """
    owner = store.ensure_principal("lazy-broken")
    _entries(store, owner.id, 1)

    def load():
        raise EmbedderUnavailable("fastembed is not installed")

    with pytest.raises(EmbedderUnavailable):
        embed.backfill_if_pending(store, owner.id, "fake-2", load)
