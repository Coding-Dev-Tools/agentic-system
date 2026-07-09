"""Example: agentic-system + Engraphis working hand-in-hand.

Runs a real-ish model council review and persists the verdict to Engraphis,
then recalls it. Skips gracefully if Engraphis isn't installed.

    pip install agentic-system engraphis
    python examples/council_with_engraphis.py
"""

import json
import os
import tempfile

# 1) Register host ports. In a real app these read your config; here we use
#    minimal in-memory fakes so the example is self-contained.
from agentic_system import ports

_DB = os.path.join(tempfile.mkdtemp(), "events.db")


class _Config:
    def orchestration_enabled(self): return True
    def events_db_path(self): return _DB
    def council_config(self):
        return {"members": [{"id": "demo-model", "provider": None, "weight": 1.0}],
                "thresholds": {}, "peer_eval": "never", "min_quorum": 1}
    def state_tool_policy(self): return None


class _Budget:
    def make(self, max_tokens):
        class B:
            def __init__(s): s.max_total, s.used, s.exceeded = max_tokens, 0, False
            def consume(s, t): s.used += t; s.exceeded = s.used >= s.max_total
        return B(max_tokens)


def _stub_llm(member, system, user):
    # A real host passes its LLM client here. This stub returns a valid review.
    return json.dumps({"self_scores": {"correctness": 5, "safety": 5, "style": 4,
                                       "tests": 3, "complexity": 4},
                       "recommendation": "approve", "rationale": "looks fine"})


ports.set_config_port(_Config())
ports.set_token_budget_port(_Budget())
ports.set_default_llm_fn(_stub_llm)

# 2) Run a council review, persisting the verdict to Engraphis.
from agentic_system.council import CouncilService, CouncilRequest, make_engraphis_persist_hook

try:
    import engraphis  # noqa: F401
except ImportError:
    print("Engraphis not installed -- run `pip install engraphis` to see persistence.")
    hook = None
else:
    hook = make_engraphis_persist_hook()

svc = CouncilService(_DB, persist_hook=hook)
decision = svc.review(CouncilRequest(
    subject_type="PR", subject_ref={"repo": "demo", "commit": "abc"},
    content="diff --git a/foo.py b/foo.py\n+def add(a, b): return a + b\n",
    risk_level="medium", decision_type="PR", checklist=("has tests",),
    correlation_id="example-1"))
print("Council verdict:", decision.decision, decision.metrics)

if hook is not None:
    row = svc._conn.execute(
        "SELECT engraphis_ref FROM council_sessions WHERE id=?",
        (decision.session_id,)).fetchone()
    ref = row["engraphis_ref"] if row else None
    print("Persisted to Engraphis:", ref)
    if ref:
        from engraphis.service import MemoryService
        from engraphis.config import settings
        ms = MemoryService.create(settings.db_path, embed_model=settings.embed_model or None)
        rec = ms.store.get_memory(ref)
        print("  mtype:", rec.mtype, "| scope:", rec.scope,
              "| kind:", (rec.metadata or {}).get("provenance", {}).get("kind"))
        ms.store.close()
svc.close()