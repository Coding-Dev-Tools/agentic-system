#!/usr/bin/env python3
"""Example: agentic-system workflow DAG engine."""

from agentic_system.workflow import WorkflowEngine, TaskDef, WorkflowDef
from agentic_system.ports import get_config_port, set_config_port, set_token_budget_port, set_default_llm_fn


# ── Minimal host ports for demo ────────────────────────────────────────────

class DemoConfig:
    def orchestration_enabled(self): return True
    def events_db_path(self): return "/tmp/workflow_demo.db"
    def council_config(self): return None
    def state_tool_policy(self): return None
    def high_impact_tool_patterns(self): return ("deploy*", "*push*")


class DemoBudget:
    def make(self, max_tokens):
        class B:
            def __init__(s): s.max_total, s.used, s.exceeded = max_tokens, 0, False
            def consume(s, t): s.used += t; s.exceeded = s.used >= s.max_total
        return B(max_tokens)


def demo_llm(member, system, user):
    return '{"self_scores": {"correctness": 4}, "recommendation": "approve", "rationale": "demo"}'


set_config_port(DemoConfig())
set_token_budget_port(DemoBudget())
set_default_llm_fn(demo_llm)


# ── Define a workflow ──────────────────────────────────────────────────────

# Simple code review workflow
CODE_REVIEW_WF = WorkflowDef(
    name="code_review",
    tasks=(
        TaskDef("lint", outputs=("lint_report",)),
        TaskDef("type_check", outputs=("type_report",)),
        TaskDef("test", inputs=("lint_report", "type_report"), outputs=("test_report",)),
        TaskDef("security_scan", inputs=("test_report",), outputs=("sec_report",)),
        TaskDef("council_review", inputs=("sec_report",), outputs=("verdict",)),
        TaskDef("apply_fixes", inputs=("verdict",), outputs=("final_diff",)),
        TaskDef("final_verification", inputs=("final_diff",), outputs=("verified",)),
    ),
)


# ── Task executors (in real app, these call your actual tools) ──────────────

def run_lint(inputs: dict) -> dict:
    print("  🔍 Running linter...")
    return {"lint_report": "✅ No lint errors"}

def run_type_check(inputs: dict) -> dict:
    print("  🔍 Running type checker...")
    return {"type_report": "✅ Types OK"}

def run_test(inputs: dict) -> dict:
    print("  🧪 Running tests...")
    return {"test_report": "✅ 42 tests passed"}

def run_security_scan(inputs: dict) -> dict:
    print("  🔒 Running security scan...")
    return {"sec_report": "✅ No vulnerabilities"}

def run_council_review(inputs: dict) -> dict:
    print("  🏛️ Council review...")
    return {"verdict": "APPROVE", "metrics": {"agreement": 0.85}}

def apply_fixes(inputs: dict) -> dict:
    print("  🔧 Applying fixes...")
    return {"final_diff": "diff --git a/foo.py b/foo.py\n+fix()"}

def final_verification(inputs: dict) -> dict:
    print("  ✅ Final verification...")
    return {"verified": True}


def main():
    db = "/tmp/workflow_demo.db"
    engine = WorkflowEngine(db)

    # Register executors
    engine.register_executor("lint", run_lint)
    engine.register_executor("type_check", run_type_check)
    engine.register_executor("test", run_test)
    engine.register_executor("security_scan", run_security_scan)
    engine.register_executor("council_review", run_council_review)
    engine.register_executor("apply_fixes", apply_fixes)
    engine.register_executor("final_verification", final_verification)

    # Create and run workflow instance
    print("🚀 Starting code review workflow")
    inst = engine.create_instance(CODE_REVIEW_WF, {"files": ["src/auth.py", "src/api.py"]})
    print(f"Created instance: {inst.instance_id}")

    # Advance workflow step by step
    while True:
        inst = engine.advance(inst.instance_id, CODE_REVIEW_WF)
        print(f"\n📍 State: {inst.state}")

        if inst.state == "DONE":
            print("\n✅ Workflow complete!")
            break
        elif inst.state == "FAILED":
            print("\n❌ Workflow failed!")
            break
        elif inst.state == "WAITING":
            print("⏳ Waiting for external input...")
            break

        # Find and execute ready task
        for task in CODE_REVIEW_WF.tasks:
            if not engine.is_task_done(inst.instance_id, task.name):
                # Check if inputs ready (simplified)
                ready = all(inp in inst.payload for inp in task.inputs)
                if ready and inst.state == "RUNNING":
                    success, result = engine.execute_task(inst.instance_id, task, "demo-worker")
                    print(f"  Task {task.name}: {'✅' if success else '❌'} {result}")
                    break

    # Show final payload
    print(f"\n📦 Final payload: {inst.payload}")
    engine.close()


if __name__ == "__main__":
    main()