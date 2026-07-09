"""End-to-end regression: workflow DAG -> council -> event store -> status.

Drives the full loop with stubbed handlers/LLM (no real calls) and asserts the
read-only status surface reflects a completed run, a council verdict, and an
IDLE agent (regression guard for the worker terminal-state heartbeat).
"""

import json

import pytest

from agentic_system.breakers import BreakerRegistry, reset_registry_for_tests
from agentic_system.council.schemas import CouncilRequest
from agentic_system.council.service import CouncilService
from agentic_system.events import hooks as orch_hooks
from agentic_system.events.state_tables import connect, ensure_state_tables
from agentic_system.orchestration_status import collect
from agentic_system.workflow.engine import WorkflowEngine
from agentic_system.workflow.worker import WorkflowWorker


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "e2e.db")
    monkeypatch.setenv("AGENTIC_ORCHESTRATION", "1")
    monkeypatch.setenv("AGENTIC_EVENTS_DB", db)
    orch_hooks.reset_bus_for_tests()
    reset_registry_for_tests()
    yield db
    orch_hooks.reset_bus_for_tests()
    reset_registry_for_tests()


def _stub_llm(member, system, user):
    if "code-review council" in system:
        base = 4.6 if member.id == "claude-sonnet-5" else (4.2 if member.id == "gpt-4o" else 3.8)
        return json.dumps({
            "self_scores": {"correctness": base, "safety": base + 0.1, "style": base,
                            "tests": base - 0.2, "complexity": base - 0.1},
            "recommendation": "approve" if base >= 4.2 else "approve_with_nits",
            "rationale": f"stub by {member.id}",
        })
    import re
    ids = re.findall(r'"review_model_id": "([^"]+)"', user)
    return json.dumps({"scores": [{"review_model_id": i, "overall": 4.5,
                                   "justification": "stub"} for i in ids]})


def test_end_to_end_review_and_test_then_status(env):
    db = env
    bus = orch_hooks.get_bus()
    council = CouncilService(db, bus=bus,
                              members=[{"id": "claude-sonnet-5", "weight": 1.1},
                                       {"id": "gpt-4o", "weight": 1.0},
                                       {"id": "gemini-2.5-pro", "weight": 0.9}],
                              peer_eval="high_risk_only", min_quorum=2,
                              llm_fn=_stub_llm,
                              persist_hook=lambda doc: f"engraphis:stub:{doc['session_id']}")

    def h_council(t):
        params = json.loads(t["input_ref"]).get("params", {})
        req = CouncilRequest(subject_type="PR",
                             subject_ref={"run": t["workflow_run_id"], "node": t["node_id"]},
                             content="diff --git a/f b/f\n+x\n",
                             risk_level="high", decision_type=params.get("decision_type", "PR"),
                             checklist=("has tests",), correlation_id=t["workflow_run_id"])
        return f"council:{council.review(req).decision}"

    handlers = {"CODEGEN": lambda t: "mem:codegen", "TESTGEN": lambda t: "mem:testgen",
                "TEST": lambda t: "mem:tests", "ANALYZE": lambda t: "mem:analysis",
                "COUNCIL_REVIEW": h_council}

    br = BreakerRegistry(db, bus=bus)
    engine = WorkflowEngine(db, bus=bus, breakers=br)
    worker = WorkflowWorker("agent-e2e", engine, handlers, role="")
    run_id = engine.start_run("review_and_test", {"trigger": "e2e"})

    while worker.run_once():
        if engine.get_run(run_id)["status"] in ("COMPLETED", "FAILED"):
            break

    # ── assertions on the materialized state via the status surface ──
    assert engine.get_run(run_id)["status"] == "COMPLETED"
    tasks = {t["node_id"]: t for t in engine.list_tasks(run_id)}
    assert set(tasks) == {"code", "testgen", "run_tests", "analyze", "council_review"}
    assert all(t["status"] == "DONE" for t in tasks.values())

    s = collect(db, tail=60)
    assert s["exists"] and s["total_events"] >= 30
    assert s["workflow_runs"][0]["status"] == "COMPLETED"
    assert any(c["decision"] in ("APPROVE", "REWORK", "REJECT") for c in s["council_sessions"])
    assert s["council_sessions"][0]["engraphis_ref"].startswith("engraphis:stub:")
    # the agent must be IDLE in the materialized view (terminal-state heartbeat)
    conn = connect(db); ensure_state_tables(conn)
    row = conn.execute("SELECT status FROM agent_instances WHERE id=?", ("agent-e2e",)).fetchone()
    conn.close()
    assert row["status"] == "IDLE"
    assert any(a["status"] == "IDLE" for a in s["agents"])

    council.close(); worker.close(); engine.close(); br.close_conn()