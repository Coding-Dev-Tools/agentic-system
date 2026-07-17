#!/usr/bin/env python3
"""Example: 8 Specialized Council Gates with gate-specific rubrics."""

from agentic_system.council import CouncilService, CouncilRequest, make_engraphis_persist_hook
from agentic_system.council.schemas import GATE_DIMENSIONS, CouncilMember
from agentic_system.ports import get_config_port, set_config_port, set_token_budget_port, set_default_llm_fn


# ── Minimal host ports ─────────────────────────────────────────────────────
class _Config:
    def orchestration_enabled(self): return True
    def events_db_path(self): return "/tmp/council_demo.db"
    def council_config(self):
        return {
            "members": [
                {"id": "claude-sonnet-5", "provider": "anthropic", "weight": 1.1},
                {"id": "gpt-4o", "provider": "openai", "weight": 1.0},
                {"id": "gemini-2.5-pro", "provider": "openrouter", "weight": 0.9},
            ],
            "thresholds": {"min_overall": 4.0, "min_safety": 4.5, "min_tests": 3.5,
                           "min_agreement": 0.7, "reject_max_overall": 2.5},
            "peer_eval": "high_risk_only",
            "min_quorum": 2,
        }
    def state_tool_policy(self): return None

class _Budget:
    def make(self, max_tokens):
        class B:
            def __init__(s): s.max_total, s.used, s.exceeded = max_tokens, 0, False
            def consume(s, t): s.used += t; s.exceeded = s.used >= s.max_total
        return B(max_tokens)

set_config_port(_Config())
set_token_budget_port(_Budget())
set_default_llm_fn(lambda m, sys, usr: '{"self_scores": {"correctness": 5, "safety": 5, "style": 4, "tests": 4, "complexity": 4}, "recommendation": "approve", "rationale": "demo"}')


# ── Gate-specific demo diffs ────────────────────────────────────────────────

DIFFS = {
    "code_edit": """diff --git a/auth.py b/auth.py
+def verify_token(token: str) -> bool:
+    return len(token) > 10
""",
    "pr_review": """diff --git a/api.py b/api.py
+def get_user(id: int) -> User:
+    return db.query(User).filter(User.id == id).first()
""",
    "merge": """diff --git a/main.py b/main.py
+    deploy_to_production()
""",
    "delegation": """Task: Refactor user service to use new cache layer
Requirements:
- Migrate from Redis to Redis Cluster
- Maintain backward compatibility
- Add circuit breaker for cache failures
""",
    "security": """diff --git a/payment.py b/payment.py
+import os
+api_key = os.environ["STRIPE_KEY"]  # HARDCODED SECRET!
+stripe.api_key = api_key
""",
    "code_quality": """diff --git a/service.py b/service.py
+class UserService:
+    def __init__(self):
+        self.db = Database()
+        self.cache = Cache()
+        self.logger = Logger()
+        self.metrics = Metrics()
+        self.auth = Auth()
+    def get_user(self, id):
+        # 50 lines of nested if/else
+        if user := self.cache.get(id):
+            if user.active:
+                if user.verified:
+                    ...
""",
    "dependency": """# requirements.txt
requests==2.28.0  # CVE-2023-32681
django==4.1.0     # CVE-2023-43665
""",
    "architecture": """# Proposed: Move all business logic into a single 5000-line God Class
# with static methods, no dependency injection, direct DB calls everywhere
""",
}


def run_gate(gate: str, diff: str):
    """Run a single council gate review."""
    print(f"\n{'='*60}")
    print(f"GATE: {gate.upper()}")
    print(f"Dimensions: {GATE_DIMENSIONS[gate]}")
    print(f"{'='*60}")

    svc = CouncilService("/tmp/council_demo.db", persist_hook=make_engraphis_persist_hook())

    req = CouncilRequest(
        subject_type="CODE_EDIT" if gate != "dependency" else "DEPENDENCY_UPDATE",
        subject_ref={"repo": "demo", "file": "example.py"},
        content=diff,
        risk_level="high" if gate in ("security", "architecture") else "medium",
        gate=gate,
        checklist=("no secrets", "tests updated", "docs updated"),
    )

    decision = svc.review(req)
    print(f"Decision: {decision.decision}")
    print(f"Metrics:  {decision.metrics}")
    print(f"Session:  {decision.session_id}")

    if decision.per_model:
        for model, data in decision.per_model.items():
            print(f"  {model}: {data['recommendation']} (overall={data['peer_overall']:.2f})")

    svc.close()
    return decision


def main():
    print("Agentic-System Council Gates Demo")
    print("=" * 60)
    print("Showing gate-specific rubric dimensions:")
    for gate, dims in GATE_DIMENSIONS.items():
        print(f"  {gate:15} -> {dims}")

    # Run a few representative gates
    for gate in ["code_edit", "security", "merge", "architecture"]:
        run_gate(gate, DIFFS[gate])

    print("\n✅ Demo complete — each gate uses tailored rubric dimensions")


if __name__ == "__main__":
    main()