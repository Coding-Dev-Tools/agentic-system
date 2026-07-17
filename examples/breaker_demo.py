#!/usr/bin/env python3
"""Example: Three-level circuit breakers with self-heal."""

import time
from agentic_system.breakers import (
    get_registry, BreakerRegistry, BreakerRegistry, CLOSED, OPEN, HALF_OPEN,
    high_impact_block_message,
)
from agentic_system.ports import get_config_port, set_config_port, set_token_budget_port, set_default_llm_fn


# ── Minimal host ports ─────────────────────────────────────────────────────
class _Config:
    def orchestration_enabled(self): return True
    def events_db_path(self): return "/tmp/breaker_demo.db"
    def council_config(self): return None
    def state_tool_policy(self): return None

class _Budget:
    def make(self, max_tokens):
        class B:
            def __init__(s): s.max_total, s.used, s.exceeded = max_tokens, 0, False
            def consume(s, t): s.used += t; s.exceeded = s.used >= s.max_total
        return B(max_tokens)

set_config_port(_Config())
set_token_budget_port(_Budget())
set_default_llm_fn(lambda m, sys, usr: '{}')


def demo_basic_lifecycle():
    """Show basic breaker state transitions."""
    print("=== Basic Breaker Lifecycle ===")
    reg = get_registry()

    # Initially closed
    print(f"Agent 'worker-1': {reg.state('agent', 'worker-1')}")
    print(f"Workflow 'deploy': {reg.state('workflow', 'deploy')}")
    print(f"Global: {reg.state('global', 'system')}")

    # Open agent breaker
    reg.open("agent", "worker-1", "too many errors")
    print(f"\nAfter open: {reg.state('agent', 'worker-1')}")

    # Try to accept task
    print(f"Should accept task? {reg.should_accept_task(agent_id='worker-1')}")

    # Half-open (probation)
    reg.half_open("agent", "worker-1", "probation")
    print(f"After half-open: {reg.state('agent', 'worker-1')}")

    # Close (recovered)
    reg.close("agent", "worker-1", "errors stopped")
    print(f"After close: {reg.state('agent', 'worker-1')}")

    print()


def demo_global_breaker_gates():
    """Show global breaker blocking high-impact tools."""
    print("=== Global Breaker Gates High-Impact Tools ===")
    reg = get_registry()

    # Open global breaker
    reg.open("global", "system", "incident: database down")
    print(f"Global breaker: {reg.state('global', 'system')}")

    # These should be blocked
    test_cases = [
        ("deploy", {}),
        ("git_push", {"remote": "origin", "branch": "main"}),
        ("terminal", {"command": "git push origin main"}),
        ("terminal", {"command": "npm publish"}),
        ("terminal", {"command": "kubectl apply -f deploy.yaml"}),
        ("read_file", {"path": "README.md"}),  # NOT high-impact
    ]

    for tool, args in test_cases:
        blocked = high_impact_block_message(tool, args)
        status = "🚫 BLOCKED" if blocked else "✅ allowed"
        print(f"  {status} {tool} {args}")

    print()


def demo_workflow_breaker():
    """Workflow-level breaker prevents new claims."""
    print("=== Workflow Breaker ===")
    reg = get_registry()

    reg.open("workflow", "deploy-prod", "stuck deployment")
    print(f"Workflow 'deploy-prod': {reg.state('workflow', 'deploy-prod')}")
    print(f"Should accept task? {reg.should_accept_task(workflow='deploy-prod')}")

    reg.close("workflow", "deploy-prod", "deployment recovered")
    print(f"After close: {reg.should_accept_task(workflow='deploy-prod')}")
    print()


def demo_self_heal():
    """Show automatic self-heal after cooldown."""
    print("=== Self-Heal (requires time travel) ===")
    reg = get_registry()

    # Open global breaker
    reg.open("global", "system", "incident")
    print(f"Opened: {reg.state('global', 'system')}")

    # Simulate cooldown elapsed by manually backdating (demo only)
    import sqlite3
    conn = sqlite3.connect("/tmp/breaker_demo.db")
    conn.execute(
        "UPDATE breakers SET opened_at=?, half_open_at=NULL WHERE level='global' AND key='system'",
        (time.time() - 400,)  # 400 seconds ago (> 300s cooldown)
    )
    conn.commit()
    conn.close()

    # Trigger self-heal
    changes = reg.try_self_heal()
    print(f"Self-heal changes: {changes}")
    print(f"State after heal: {reg.state('global', 'system')}")
    print()


def demo_snapshot():
    """Show full breaker snapshot."""
    print("=== Full Snapshot ===")
    reg = get_registry()
    for b in reg.snapshot():
        marker = "🔴" if b["state"] == OPEN else ("🟡" if b["state"] == HALF_OPEN else "🟢")
        print(f"  {marker} {b['level']:>10} | {b['key']:<30} {b['state']:<8} ({b.get('reason','')})")


def main():
    demo_basic_lifecycle()
    demo_global_breaker_gates()
    demo_workflow_breaker()
    demo_self_heal()
    demo_snapshot()


if __name__ == "__main__":
    main()