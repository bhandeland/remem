"""The embedder seam.

Mirrors store.py: a Protocol with one implementation shipped. The tests here
use a fake, deliberately - a test that downloads a 130MB ONNX model is a test
that fails on a plane, and what needs proving at this layer is the contract,
not the arithmetic of a particular model.
"""

import pytest

from remem.embed import Embedder, EmbedderUnavailable, load_embedder


class FakeEmbedder:
    """Deterministic and dimensionally honest. Not a good embedder - a
    correct one, in the only sense the callers care about."""

    name = "fake-2"
    dim = 2

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t)), 1.0] for t in texts]


def test_fake_satisfies_the_protocol():
    # Protocol is runtime_checkable so services can assert on it.
    assert isinstance(FakeEmbedder(), Embedder)


def test_embed_returns_one_vector_per_text():
    out = FakeEmbedder().embed(["a", "bb", "ccc"])
    assert len(out) == 3
    assert all(len(v) == 2 for v in out)


def test_embed_of_empty_list_is_empty():
    # The batching loop in services/embed.py can legitimately hand over an
    # empty batch; it must not become a model call or an error.
    assert FakeEmbedder().embed([]) == []


def test_unknown_backend_raises_embedder_unavailable():
    with pytest.raises(EmbedderUnavailable) as exc:
        load_embedder("no-such-model-anywhere")
    # The message has to name the install command. This error surfaces in a
    # cron log, where a bare "unavailable" costs an hour.
    assert "uv tool install" in str(exc.value)
