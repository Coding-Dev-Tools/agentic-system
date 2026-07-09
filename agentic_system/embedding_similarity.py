"""Optional semantic similarity for NoProgressDetector, backed by
sentence-transformers (the ``[embeddings]`` extra).

The default :class:`no_progress.NoProgressDetector` uses stdlib difflib, which
catches verbatim/near-verbatim loops. For *semantic* looping (an agent producing
different words that mean the same thing over and over), pass an embedding
similarity::

    from agentic_system.embedding_similarity import make_embedding_similarity
    det = NoProgressDetector(similarity=make_embedding_similarity())

The model is loaded lazily on the first call so importing this module is cheap.
Requires ``pip install "agentic-system[embeddings]"`` (adds sentence-transformers).

Engraphis note: if you already run Engraphis with its embedder loaded, you can
build an equivalent ``similarity`` callable against its embedder instead of a
separate sentence-transformers model — the ``NoProgressDetector`` seam only
needs a ``(a, b) -> float`` callable.
"""

from __future__ import annotations

from typing import Optional

from .no_progress import SimilarityFn


class _SentenceTransformerSimilarity:
    """Cosine similarity over sentence-transformer embeddings (lazy model)."""

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
        self._model_name = model_name
        self._model = None  # loaded on first use

    def _ensure_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self._model_name)
        return self._model

    def __call__(self, a: str, b: str) -> float:
        import math
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        m = self._ensure_model()
        emb = m.encode([a, b], convert_to_numpy=True, normalize_embeddings=True)
        # embeddings are L2-normalized -> dot product == cosine similarity
        cos = float(emb[0] @ emb[1])
        if math.isnan(cos):
            return 0.0
        return max(0.0, min(1.0, cos))


def make_embedding_similarity(
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2") -> SimilarityFn:
    """Return a ``(a, b) -> float`` similarity callable backed by
    sentence-transformers. The model loads on the first call. Requires the
    ``[embeddings]`` extra (``pip install "agentic-system[embeddings]"``)."""
    return _SentenceTransformerSimilarity(model_name)


__all__ = ["make_embedding_similarity"]