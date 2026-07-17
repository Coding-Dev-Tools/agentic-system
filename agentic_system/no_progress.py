"""No-progress loop detection for agents.

The default ``NoProgressDetector`` uses difflib (catches verbatim loops).
For semantic looping, back it with embeddings — either sentence-transformers
or your own callable against Engraphis's embedder.

Usage::

    from agentic_system.no_progress import NoProgressDetector
    from agentic_system.embedding_similarity import make_embedding_similarity

    # Deterministic (stdlib only):
    det = NoProgressDetector(window=3, threshold=0.9)

    # Semantic (requires sentence-transformers or custom callable):
    det = NoProgressDetector(window=3, threshold=0.85,
                             similarity=make_embedding_similarity())
"""

from __future__ import annotations

import difflib
from typing import Callable, Optional

try:
    from agentic_system.ports import get_config_port
except Exception:
    def get_config_port():
        class _Dummy:
            def orchestration_enabled(self): return False
        return _Dummy()


class SimilarityFn(Callable[[str, str], float]):
    """Callable that returns a similarity score in [0, 1] for two texts."""
    pass


def _difflib_similarity(a: str, b: str) -> float:
    """Stdlib difflib ratio — fast, catches verbatim/near-verbatim loops."""
    return difflib.SequenceMatcher(None, a, b).ratio()


class NoProgressDetector:
    """Sliding-window no-progress detector.

    Args:
        window: How many recent turns to compare (2-10 typical).
        threshold: Similarity threshold in [0, 1]. Above = loop.
        similarity: Callable(a, b) -> float in [0, 1]. Defaults to difflib.
    """
    def __init__(self, window: int = 3, threshold: float = 0.9,
                 similarity: Optional[SimilarityFn] = None):
        if not (2 <= window <= 10):
            raise ValueError("window must be 2..10")
        if not (0.0 <= threshold <= 1.0):
            raise ValueError("threshold must be 0..1")
        self.window = window
        self.threshold = threshold
        self.similarity = similarity or _difflib_similarity
        self._history: list[str] = []

    def record(self, output: str) -> bool:
        """Record a turn's output. Returns True if a loop is detected."""
        self._history.append(output)
        if len(self._history) > self.window:
            self._history.pop(0)
        if len(self._history) < self.window:
            return False
        # Compare the newest against each prior in the window
        newest = self._history[-1]
        for prior in self._history[:-1]:
            if self.similarity(newest, prior) >= self.threshold:
                return True
        return False

    def reset(self) -> None:
        self._history.clear()


def make_no_progress_detector() -> NoProgressDetector:
    """Factory using ConfigPort (host may override threshold/window/embedding)."""
    try:
        cfg = get_config_port()
        if cfg.orchestration_enabled():
            cc = cfg.council_config() or {}
            np = cc.get("no_progress", {})
            window = int(np.get("window", 3))
            threshold = float(np.get("threshold", 0.9))
            return NoProgressDetector(window=window, threshold=threshold)
    except Exception:
        pass
    return NoProgressDetector()