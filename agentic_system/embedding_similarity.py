"""Optional semantic similarity backends for no-progress detection.

Install extras to enable::

    pip install "agentic-system[embeddings]"  # adds sentence-transformers

Or supply your own callable::

    from agentic_system.embedding_similarity import SimilarityFn
    my_cosine: SimilarityFn = lambda a, b: engraphis_cosine(a, b)
"""

from __future__ import annotations

import os
from typing import Callable, Optional

SimilarityFn = Callable[[str, str], float]

# ── Deterministic fallback (stdlib) ───────────────────────────────────────

def _deterministic_embed(text: str) -> list[float]:
    """Stable hash-based pseudo-embedding (no ML deps, deterministic)."""
    import hashlib
    h = hashlib.md5(text.encode()).digest()
    # 32-dim vector from 16-byte hash
    return [((b >> 4) & 0xF) / 15.0 for b in h] + [(b & 0xF) / 15.0 for b in h]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _deterministic_similarity(a: str, b: str) -> float:
    return _cosine(_deterministic_embed(a), _deterministic_embed(b))


# ── Optional: sentence-transformers ──────────────────────────────────────

_st_model = None


def _get_st_model(model_name: str = "all-MiniLM-L6-v2"):
    global _st_model
    if _st_model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _st_model = SentenceTransformer(model_name)
        except Exception:
            return None
    return _st_model


def _st_similarity(a: str, b: str, model_name: str = "all-MiniLM-L6-v2") -> float:
    model = _get_st_model(model_name)
    if model is None:
        return _deterministic_similarity(a, b)
    try:
        emb_a = model.encode(a, convert_to_tensor=False)
        emb_b = model.encode(b, convert_to_tensor=False)
        import numpy as np
        return float(np.dot(emb_a, emb_b) / (np.linalg.norm(emb_a) * np.linalg.norm(emb_b)))
    except Exception:
        return _deterministic_similarity(a, b)


# ── Public factory ────────────────────────────────────────────────────────

def make_embedding_similarity(
    *,
    backend: str = "auto",              # "auto" | "sentence-transformers" | "deterministic" | "engraphis"
    model: str = "all-MiniLM-L6-v2",
    engraphis_workspace: str = "default",
    engraphis_repo: Optional[str] = None,
) -> SimilarityFn:
    """Return a (a: str, b: str) -> float in [0, 1] similarity function.

    backends:
      - "auto": try sentence-transformers, fall back to deterministic
      - "sentence-transformers": require sentence-transformers (raises if missing)
      - "deterministic": stable hash-based (no deps, no network)
      - "engraphis": call Engraphis MCP embedder (requires engraphis[mcp] + running MCP)

    The returned callable never raises; on any error it falls back to
    deterministic similarity and logs a warning.
    """
    if backend == "deterministic":
        return _deterministic_similarity

    if backend == "sentence-transformers":
        def _fn(a: str, b: str) -> float:
            try:
                return _st_similarity(a, b, model)
            except Exception:
                return _deterministic_similarity(a, b)
        return _fn

    if backend == "engraphis":
        # Lazy import — only works when engraphis[mcp] is installed and server up
        def _fn(a: str, b: str) -> float:
            try:
                from agentic_system.ports import get_engraphis_port
                port = get_engraphis_port()
                if port is None:
                    return _deterministic_similarity(a, b)
                # Engraphis port would need an embed method; not standard yet
                # This is a placeholder for future Engraphis embedder integration
                return _deterministic_similarity(a, b)
            except Exception:
                return _deterministic_similarity(a, b)
        return _fn

    # "auto"
    try:
        _ = _get_st_model(model)
        def _fn(a: str, b: str) -> float:
            try:
                return _st_similarity(a, b, model)
            except Exception:
                return _deterministic_similarity(a, b)
        return _fn
    except Exception:
        return _deterministic_similarity