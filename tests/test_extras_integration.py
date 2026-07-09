"""Integration tests for the optional extras: the no-progress similarity seam,
the [embeddings] sentence-transformer backing, and the [engraphis] council
persistence hook. Each skips gracefully when its extra isn't installed.
"""

import sys

import pytest

from agentic_system.no_progress import NoProgressDetector, default_similarity


# ── no-progress similarity seam (no extra needed) ──────────────────────────

def test_default_similarity_is_difflib():
    assert default_similarity("abc", "abc") == 1.0
    assert 0.0 <= default_similarity("abc", "xyz") < 1.0


def test_similarity_seam_uses_injected_callable():
    calls = []
    def fake_sim(a, b):
        calls.append((a, b))
        return 0.99  # always "similar"
    det = NoProgressDetector(window=2, threshold=0.9, similarity=fake_sim)
    assert det.observe("first output") is False     # window not full
    assert det.observe("second output") is True       # fake_sim -> 0.99 >= 0.9
    assert len(calls) == 1                            # one pairwise comparison


def test_similarity_seam_can_prevent_false_trip():
    # difflib would trip on near-verbatim repeats; a strict fake similarity can
    # refuse to trip, proving the seam actually drives the verdict.
    def strict_sim(a, b):
        return 1.0 if a == b else 0.0
    det = NoProgressDetector(window=3, threshold=0.97, similarity=strict_sim)
    assert det.observe("almost same 1") is False
    assert det.observe("almost same 2") is False
    assert det.observe("almost same 3") is False     # not identical -> 0.0 < 0.97


# ── [embeddings] sentence-transformer backing ──────────────────────────────

def test_embedding_similarity_factory_is_lazy():
    st = pytest.importorskip("sentence_transformers")
    from agentic_system.embedding_similarity import make_embedding_similarity
    sim = make_embedding_similarity()      # must NOT load the model yet
    assert callable(sim)
    # first call loads the model and returns a float in [0, 1]
    try:
        val = sim("the agent is stuck", "the agent is stuck")
    except Exception as e:  # model download unavailable in this env -> skip
        pytest.skip(f"sentence-transformer model unavailable: {e}")
    assert isinstance(val, float) and 0.0 <= val <= 1.0
    assert val >= 0.95


# ── [engraphis] council persistence hook ────────────────────────────────────

def test_persist_hook_noop_when_engraphis_unavailable(monkeypatch):
    # simulate engraphis not being installed
    for mod in ("engraphis", "engraphis.service", "engraphis.config"):
        monkeypatch.setitem(sys.modules, mod, None)
    from agentic_system.council.service import make_engraphis_persist_hook
    hook = make_engraphis_persist_hook()
    assert hook({"session_id": "s", "decision": "APPROVE",
                 "subject_type": "PR", "session": {}}) is None