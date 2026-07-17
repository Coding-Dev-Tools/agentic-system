#!/usr/bin/env python3
"""Example: Circuit breakers (3 levels + high-impact tool gate)."""

from agentic_system.breakers import (
    BreakerRegistry, get_registry, CLOSED, OPEN, HALF_OPEN,
    high_impact_block_message, HIGH_IMPACT_PATTERNS,
)
from agentic_system.ports import get_config_port, set_config_port, set_token_budget_port, set_default_llm_fn


# ── Minimal host ports ──────────────────────────────────────────────────────

class DemoConfig:
    def orchestration_enabled(self): return True
    def events_db_path(self): return "/tmp/breakers_demo.db"
    def council_config(self): return None
    def state_tool_policy(self): return None
    def high_impact_tool_patterns(self): return ("deploy*", "*git_push*", "publish*")


class DemoBudget:
    def make(self, max_tokens):
        class B:
            def __init__(s): s.max_total, s.used, s.exceeded = max_tokens, 0, False
            def consume(s, t): s.used += t; s.exceeded = s.used >= s.max_total
        return B(max_tokens)


def demo_llm(m, sys, usr): return '{"self_scores": {"correctness": 4}, "recommendation": "approve", "rationale": "demo"}'


set_config_port(DemoConfig())
set_token_budget_port(DemoBudget())
set_default_llm_fn(demo_llm)


# ── Demo 1: Basic breaker lifecycle ────────────────────────────────────────

def demo_lifecycle():
    print("=== 1. Breaker Lifecycle ===")
    reg = get_registry("/tmp/breakers_demo.db")

    # Initially closed
    print(f"  Agent 'worker-1': {reg.state('agent', 'worker-1')}")

    # Open on repeated failures
    reg.open("agent", "worker-1", "5 consecutive task failures")
    print(f"  After failures: {reg.state('agent', 'worker-1')}")

    # Try to accept task
    can_accept = reg.should_accept_task(agent_id="worker-1")
    print(f"  Should accept task: {can_accept}")

    # Half-open (probation)
    reg.half_open("agent", "worker-1", "probation after cooldown")
    print(f"  Half-open: {reg.state('agent', 'worker-1')}")

    # Close on success
    reg.close("agent", "worker-1", "task succeeded")
    print(f"  Recovered: {reg.state('agent', 'worker-1')}")

    print()


# ── Demo 2: Global breaker blocks high-impact tools ────────────────────────

def demo_high_impact_gate():
    print("=== 2. High-Impact Tool Gate ===")
    reg = get_registry("/tmp/breakers_demo.db")

    # Open global breaker (incident!)
    reg.open("global", "system", "deployment pipeline degraded")
    print(f"  Global breaker: {reg.state('global', 'system')}")

    # These should be blocked
    test_cases = [
        ("deploy", {}),
        ("git_push", {"remote": "origin", "branch": "main"}),
        ("publish_npm", {}),
        ("terminal", {"command": "git push origin main"}),
        ("terminal", {"command": "npm publish"}),
        ("terminal", {"command": "kubectl apply -f deployment.yaml"}),
        ("read_file", {"path": "src/main.py"}),  # NOT high-impact
    ]

    for tool, args in test_cases:
        blocked = high_impact_block_message(tool, args)
        status = "🚫 BLOCKED" if blocked else "✅ allowed"
        print(f"  {tool:15} {status}")

    # Close global breaker
    reg.close("global", "system", "pipeline recovered")
    print(f"\n  After recovery: {reg.state('global', 'system')}")

    # Now should be allowed
    blocked = high_impact_block_message("deploy", {})
    print(f"  deploy now: {'🚫 BLOCKED' if blocked else '✅ allowed'}")
    print()


# ── Demo 3: Workflow-level breaker ────────────────────────────────────────

def demo_workflow_breaker():
    print("=== 3. Workflow-Level Breaker ===")
    reg = get_registry("/tmp/breakers_demo.db")

    # Open breaker for specific workflow
    reg.open("workflow", "code_review", "council API unavailable")
    print(f"  code_review workflow: {reg.state('workflow', 'code_review')}")

    # Should block tasks in that workflow
    can_accept = reg.should_accept_task(workflow="code_review")
    print(f"  Should accept code_review task: {can_accept}")

    # Other workflows unaffected
    can_accept = reg.should_accept_task(workflow="deployment")
    print(f"  Should accept deployment task: {can_accept}")

    # Close workflow breaker
    reg.close("workflow", "code_review", "council API restored")
    print(f"  After fix: {reg.state('workflow', 'code_review')}")
    print()


# ── Demo 4: Auto-recovery (self-heal) ────────────────────────────────────

def demo_self_heal():
    print("=== 4. Self-Heal (Auto-Recovery) ===")
    reg = get_registry("/tmp/breakers_demo.db")

    # Open global breaker
    reg.open("global", "system", "incident started")
    print(f"  Global breaker: {reg.state('global', 'system')}")

    # Manually trigger self-heal (in production, runs on timer)
    changes = reg.try_self_heal()
    print(f"  Self-heal changes: {changes}")

    # With short cooldown for demo
    reg.global_auto_recover_seconds = 0  # instant for demo
    changes = reg.try_self_heal()
    print(f"  After cooldown: {reg.state('global', 'system')}")
    print()


# ── Demo 5: Snapshot & monitoring ────────────────────────────────────────

def demo_snapshot():
    print("=== 5. Snapshot & Monitoring ===")
    reg = get_registry("/tmp/breakers_demo.db")

    # Add some breakers
    reg.open("agent", "worker-42", "task timeout")
    reg.half_open("workflow", "pr_merge", "probation")
    reg.open("global", "system", "incident")

    snap = reg.snapshot()
    print("  Current breakers:")
    for b in snap:
        marker = "🔴" if b["state"] == "OPEN" else ("🟡" if b["state"] == "HALF_OPEN" else "🟢")
        print(f"  {marker} {b['level']:>10} | {b['key']:<20} {b['state']} ({b.get('reason','')[:40]})")

    # High-impact patterns
    print(f"\n  High-impact patterns: {HIGH_IMPACT_PATTERNS}")
    print()


def main():
    print("Circuit Breakers Demo")
    print("=" * 50)

    demo_lifecycle()
    demo_high_impact_gate()
    demo_workflow_breaker()
    demo_self_heal()
    demo_snapshot()

    print("Key Points:")
    print("  • 3 levels: agent → workflow → global")
    print("  • Global OPEN blocks deploy/push/publish (incl. inside terminal)")
    print("  • Auto-recovery after configurable cooldown")
    print("  • Task success auto-closes agent/workflow breakers")
    print("  • Snapshot API for monitoring dashboards")


if __name__ == "__main__":
    main()