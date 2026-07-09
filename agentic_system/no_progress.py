"""No-progress (loop) detection for agent tool-call cycles (handoff §3.6).

Tracks a small window of recent tool outputs; when every pairwise similarity
in the window stays above a threshold AND no new state was written, the agent
is considered to be looping and the task should fail with
``reason="no_progress"``.

Similarity is stdlib ``difflib.SequenceMatcher`` — dependency-free and good
enough for verbatim/near-verbatim repetition, which is what runaway agent
loops actually produce. Upgrade path: swap ``_similarity`` for Engraphis'
embedding cosine similarity if semantic-level looping shows up in practice
(reuse its embedder; do not add a new embedding stack).
"""

from __future__ import annotations

import re
from collections import deque
from difflib import SequenceMatcher

_WS = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WS.sub(" ", (text or "").strip())


def _similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


class NoProgressDetector:
    """Sliding-window repetition detector.

    Usage::

        det = NoProgressDetector(window=3, threshold=0.97)
        looping = det.observe(tool_output, state_changed=wrote_anything)
        if looping:
            fail_task(reason="no_progress")
    """

    def __init__(self, window: int = 3, threshold: float = 0.97,
                 max_chars: int = 4000):
        if window < 2:
            raise ValueError("window must be >= 2")
        self.window = window
        self.threshold = float(threshold)
        self.max_chars = int(max_chars)
        self._outputs: deque[str] = deque(maxlen=window)
        self.trip_count = 0

    def observe(self, output: str, state_changed: bool = False) -> bool:
        """Record one tool output. Returns True when a loop is detected.

        ``state_changed=True`` means the step produced a real side effect
        (file written, task state advanced, new event appended) — that resets
        the window, since repeated *reads* with identical output are only a
        loop when nothing is being accomplished between them.
        """
        text = _normalize(output)[: self.max_chars]
        if state_changed:
            self._outputs.clear()
            self._outputs.append(text)
            return False
        self._outputs.append(text)
        if len(self._outputs) < self.window:
            return False
        outs = list(self._outputs)
        sims = [
            _similarity(outs[i], outs[j])
            for i in range(len(outs))
            for j in range(i + 1, len(outs))
        ]
        if min(sims) >= self.threshold:
            self.trip_count += 1
            return True
        return False

    def reset(self) -> None:
        self._outputs.clear()


__all__ = ["NoProgressDetector"]
