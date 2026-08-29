"""The embedding seam. A Protocol, and the one local implementation shipped.

Same shape as store.py and for the same reason: the thing behind this
interface is a plausible future substitution, and naming the interface now
costs nothing while retrofitting one later costs every call site.

The shipped implementation is local and ONNX-based. No API key, no network on
any read path, no per-call cost, and every existing entry backfillable
without asking anyone's permission. A hosted embedder is a configuration
question for someone else's package, not a dependency of this one.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

#: bge-small-en-v1.5: 384 dimensions, ~130MB of ONNX weights, and near the
#: top of the retrieval benchmarks for its size. Chosen for the size, which
#: is what keeps `remem` installable on a laptop without PyTorch.
DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"


@runtime_checkable
class Embedder(Protocol):
    name: str
    """Recorded in entry_vectors.model. Changing it makes every existing
    vector stale rather than wrong - the old rows stay, readable, until
    something deletes them."""

    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbedderUnavailable(RuntimeError):
    """No embedder could be loaded.

    Raised rather than degraded, because the caller is `remem embed`, whose
    entire job this is. Search degrades instead: its semantic tier finds no
    vectors and falls through to trigram, which is the correct behaviour
    there and the wrong behaviour here.
    """


class LocalEmbedder:
    """fastembed over ONNX Runtime. Imported lazily, on purpose.

    fastembed pulls onnxruntime, which is tens of megabytes and takes a
    noticeable moment to import. Every `remem` invocation would pay that -
    including the hooks, which are meant to be invisible - if this were a
    module-level import. It is deferred to first use, which is the embed
    command and the semantic search tier.
    """

    def __init__(self, model_name: str = DEFAULT_EMBED_MODEL) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise EmbedderUnavailable(
                "The local embedder needs the 'embed' extra. Install it with "
                "`uv tool install --editable '.[embed]'` and re-run."
            ) from exc

        try:
            self._model = TextEmbedding(model_name=model_name)
        except Exception as exc:
            # fastembed raises assorted types for an unknown model name, a
            # failed download, and a corrupt cache. They are one situation to
            # the caller, and the model name is the part worth reporting.
            raise EmbedderUnavailable(
                f"Could not load embedding model {model_name!r}: {exc}. "
                "If this is a name typo, fix REMEM_EMBED_MODEL; if it is a "
                "download failure, re-run `remem embed` when online. "
                "Install the extra with `uv tool install --editable '.[embed]'`."
            ) from exc

        self.name = model_name
        # Asked of the model rather than hardcoded: a wrong constant here
        # would not fail until pgvector rejected the dimension, several
        # layers away from the mistake.
        self.dim = len(next(iter(self._model.embed(["dimension probe"]))))

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            # A model call on an empty batch is wasted startup at best and an
            # error at worst, and the batching loop legitimately produces one.
            return []
        return [list(map(float, v)) for v in self._model.embed(texts)]


def load_embedder(model_name: str = DEFAULT_EMBED_MODEL) -> Embedder:
    """The one place that decides which implementation to build.

    A single branch today. It exists so that adding a second implementation
    is a change here and nowhere else.
    """
    return LocalEmbedder(model_name)
