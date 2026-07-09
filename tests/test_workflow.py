"""Phase 3 tests: DAG definitions, engine lifecycle, restart-resume, worker."""

import pytest

from agentic_system.breakers import BreakerRegistry, GLOBAL_KEY
from agentic_system.events.store import EventStore
from agentic_system.workflow.definitions import (
    WorkflowDefinitionError, from_dict, load_directory,
)
from agentic_system.workflow.engine import WorkflowEngine
from agentic_system.workflow.worker import WorkflowWorker

LINEAR = {
    "name": "linear",
    "nodes": [
        {"id": "a", "task_type": "CODEGEN"},
        {"id": "b", "task_type": "TEST", "depends_on": ["a"], "max_attempts": 2},
    ],
}

DIAMOND = {
    "name": "diamond",
    "nodes": [
        {"id": "code", "task_type": "CODEGEN"},
        {"id": "tests", "task_type": "TEST", "depends_on": ["code"]},
        {"id": "lint", "task_type": "ANALYZE", "depends_on": ["code"]},
        {"id": "review", "task_type": "COUNCIL_REVIEW", "depends_on": ["tests", "lint"]},
    ],
}


def _engine(tmp_path, extra=None, **kw):
    defs = {d["name"]: from_dict(d) for d in [LINEAR, DIAMOND] + (extra or [])}
    return WorkflowEngine(str(tmp_path / "wf.db"), definitions=defs, **kw)


# ── definitions ───────────────────────────────────────────────────────────

def test_cycle_rejected():
    with pytest.raises(WorkflowDefinitionError):
        from_dict({"name": "c", "nodes": [
            {"id": "a", "task_type": "X", "depends_on": ["b"]},
            {"id": "b", "task_type": "X", "depends_on": ["a"]}]})


def test_unknown_dep_rejected():
    with pytest.raises(WorkflowDefinitionError):
        from_dict({"name": "u", "nodes": [{"id": "a", "task_type": "X",
                                           "depends_on": ["ghost"]}]})


def test_shipped_yaml_definitions_load():
    defs = load_directory()
    assert {"refactor_sweep", "review_and_test"} <= set(defs)
    assert defs["review_and_test"].topo_order()[-1] == "council_review"


# ── engine lifecycle ──────────────────────────────────────────────────────

def test_linear_run_end_to_end(tmp_path):
    eng = _engine(tmp_path)
    run_id = eng.start_run("linear", {"repo": "demo"})
    tasks = eng.list_tasks(run_id)
    assert len(tasks) == 1 and tasks[0]["node_id"] == "a"  # b gated on a
    assert eng.claim_task(tasks[0]["id"], "agent-1")
    eng.complete_task(tasks[0]["id"], output_ref="mem-1")
    tasks = eng.list_tasks(run_id)
    b = next(t for t in tasks if t["node_id"] == "b")
    assert b["status"] == "PENDING"
    assert eng.claim_task(b["id"], "agent-2")
    eng.complete_task(b["id"])
    run = eng.get_run(run_id)
    assert run["status"] == "COMPLETED" and run["current_node_id"] is None
    store = EventStore(eng.db_path)
    types = [e.type for _, e in store.read_since(0)]
    for expected in ["workflow.run_started", "workflow.node_ready", "task.created",
                     "task.claimed", "task.completed", "workflow.run_completed"]:
        assert expected in types
    store.close(); eng.close()


def test_diamond_join_gates_on_both_legs(tmp_path):
    eng = _engine(tmp_path)
    run_id = eng.start_run("diamond")
    code = eng.list_tasks(run_id)[0]
    eng.claim_task(code["id"], "a1"); eng.complete_task(code["id"])
    by_node = {t["node_id"]: t for t in eng.list_tasks(run_id)}
    assert by_node["tests"]["status"] == "PENDING"
    assert by_node["lint"]["status"] == "PENDING"
    assert "review" not in by_node
    eng.claim_task(by_node["tests"]["id"], "a1"); eng.complete_task(by_node["tests"]["id"])
    assert "review" not in {t["node_id"] for t in eng.list_tasks(run_id)}  # lint pending
    by_node = {t["node_id"]: t for t in eng.list_tasks(run_id)}
    eng.claim_task(by_node["lint"]["id"], "a1"); eng.complete_task(by_node["lint"]["id"])
    by_node = {t["node_id"]: t for t in eng.list_tasks(run_id)}
    assert by_node["review"]["status"] == "PENDING"  # join satisfied
    eng.close()


def test_cas_claim_single_winner(tmp_path):
    eng = _engine(tmp_path)
    run_id = eng.start_run("linear")
    t = eng.list_tasks(run_id)[0]
    assert eng.claim_task(t["id"], "racer-1")
    assert not eng.claim_task(t["id"], "racer-2")
    assert eng.get_task(t["id"])["assigned_agent_id"] == "racer-1"
    eng.close()


def test_fail_requeue_then_exhaust_fails_run(tmp_path):
    eng = _engine(tmp_path)
    run_id = eng.start_run("linear")
    a = eng.list_tasks(run_id)[0]
    eng.claim_task(a["id"], "w1"); eng.complete_task(a["id"])
    b = next(t for t in eng.list_tasks(run_id) if t["node_id"] == "b")
    eng.claim_task(b["id"], "w1")
    assert eng.fail_task(b["id"], "flaky test") == "requeued"  # attempt 1/2
    eng.claim_task(b["id"], "w2")
    assert eng.fail_task(b["id"], "flaky again") == "failed"   # attempt 2/2
    run = eng.get_run(run_id)
    assert run["status"] == "FAILED"
    store = EventStore(eng.db_path)
    types = [e.type for _, e in store.read_since(0)]
    assert "council.escalation_requested" in types
    assert "workflow.run_failed" in types
    store.close(); eng.close()


def test_restart_resume_from_tables(tmp_path):
    """Acceptance (handoff §5 phase 3): a run survives a process restart."""
    eng1 = _engine(tmp_path)
    run_id = eng1.start_run("linear", {"repo": "demo"})
    a = eng1.list_tasks(run_id)[0]
    eng1.claim_task(a["id"], "w1"); eng1.complete_task(a["id"])
    assert eng1.get_run(run_id)["current_node_id"] == "b"
    eng1.close()  # pm2 kill

    eng2 = _engine(tmp_path)  # new process, same DB
    run = eng2.get_run(run_id)
    assert run["status"] == "RUNNING" and run["current_node_id"] == "b"
    eng2.advance(run_id)  # idempotent — does not duplicate tasks
    tasks = eng2.list_tasks(run_id)
    assert len(tasks) == 2
    b = next(t for t in tasks if t["node_id"] == "b")
    assert eng2.claim_task(b["id"], "w2")
    eng2.complete_task(b["id"])
    assert eng2.get_run(run_id)["status"] == "COMPLETED"
    eng2.close()


def test_breaker_blocks_start_and_claim(tmp_path):
    db = str(tmp_path / "wf.db")
    breakers = BreakerRegistry(db, alert_path=str(tmp_path / "ALERT.md"))
    defs = {d["name"]: from_dict(d) for d in [LINEAR]}
    eng = WorkflowEngine(db, definitions=defs, breakers=breakers)
    run_id = eng.start_run("linear")
    t = eng.list_tasks(run_id)[0]
    breakers.open("global", GLOBAL_KEY, "cost spike")
    assert not eng.claim_task(t["id"], "w1")
    with pytest.raises(RuntimeError):
        eng.start_run("linear")
    breakers.close("global", GLOBAL_KEY)
    assert eng.claim_task(t["id"], "w1")
    breakers.close_conn(); eng.close()


# ── worker ────────────────────────────────────────────────────────────────

def test_worker_runs_workflow_to_completion(tmp_path):
    eng = _engine(tmp_path)
    run_id = eng.start_run("linear", {"repo": "demo"})
    done = []
    handlers = {
        "CODEGEN": lambda task: done.append(("code", task["node_id"])) or "mem-code",
        "TEST": lambda task: done.append(("test", task["node_id"])) or "mem-test",
    }
    w = WorkflowWorker("worker-1", eng, handlers, role="")
    assert w.run_once()  # a
    assert w.run_once()  # b
    assert not w.run_once()  # nothing left
    assert eng.get_run(run_id)["status"] == "COMPLETED"
    assert [d[0] for d in done] == ["code", "test"]
    store = EventStore(eng.db_path)
    types = [e.type for _, e in store.read_since(0)]
    assert "agent.state_changed" in types  # FSM wired to the bus
    store.close(); w.close(); eng.close()


def test_worker_failure_goes_to_cooldown_and_requeues(tmp_path):
    eng = _engine(tmp_path)
    eng.start_run("linear")

    def boom(task):
        raise RuntimeError("provider outage")

    w = WorkflowWorker("worker-1", eng, {"CODEGEN": boom, "TEST": boom},
                       cooldown_seconds=0.0)
    w.run_once()
    from agentic_system.state_machine import AgentState
    assert w.fsm.state in (AgentState.COOLDOWN, AgentState.IDLE)
    # task went back to PENDING with attempts=1
    t = [t for t in eng._conn.execute("SELECT * FROM tasks").fetchall()][0]
    assert t["status"] == "PENDING" and t["attempts"] == 1
    # cooldown elapsed (0s) -> can work again
    assert w.fsm.can_accept_task()
    w.close(); eng.close()


def test_worker_materializes_terminal_idle_state(tmp_path):
    """After a run completes the worker's agent_instances row must reflect IDLE,
    not a stale EXECUTING (regression: the FSM reset to IDLE was never
    heartbeated, so the status surface / heartbeat_sweep misreported state)."""
    from agentic_system.events.state_tables import connect, ensure_state_tables
    eng = _engine(tmp_path)
    run_id = eng.start_run("linear")
    w = WorkflowWorker("worker-idle", eng,
                        {"CODEGEN": lambda t: "mem", "TEST": lambda t: "mem"}, role="")
    while w.run_once():
        pass
    assert eng.get_run(run_id)["status"] == "COMPLETED"
    conn = connect(eng.db_path); ensure_state_tables(conn)
    row = conn.execute("SELECT status FROM agent_instances WHERE id=?",
                       ("worker-idle",)).fetchone()
    conn.close(); w.close(); eng.close()
    assert row is not None
    assert row["status"] == "IDLE"


def test_worker_materializes_cooldown_state_on_failure(tmp_path):
    """A failed task leaves the agent in COOLDOWN in agent_instances."""
    from agentic_system.events.state_tables import connect, ensure_state_tables
    eng = _engine(tmp_path)
    eng.start_run("linear")
    def _boom(t):
        raise RuntimeError("boom")
    w = WorkflowWorker("worker-fail", eng,
                        {"CODEGEN": _boom, "TEST": lambda t: "mem"},
                        cooldown_seconds=60.0)
    w.run_once()
    conn = connect(eng.db_path); ensure_state_tables(conn)
    row = conn.execute("SELECT status FROM agent_instances WHERE id=?",
                       ("worker-fail",)).fetchone()
    conn.close(); w.close(); eng.close()
    assert row is not None and row["status"] == "COOLDOWN"


def test_worker_no_progress_drives_dedicated_fsm_event(tmp_path):
    """A handler raising NoProgress maps onto the FSM ``no_progress`` event
    (EXECUTING -> FAILED), not ``tool_error``, and is recorded with reason
    no_progress."""
    from agentic_system.no_progress import NoProgress
    from agentic_system.state_machine import AgentState

    eng = _engine(tmp_path)
    eng.start_run("linear")

    det_calls = {"n": 0}

    def looping(task):
        # a handler that detects a loop and raises NoProgress (e.g. via
        # detector.raise_if_looping); the worker bridges it to the FSM.
        det_calls["n"] += 1
        raise NoProgress("identical output 3x in a row")

    w = WorkflowWorker("worker-np", eng, {"CODEGEN": looping, "TEST": looping},
                        cooldown_seconds=0.0)
    w.run_once()
    assert w.fsm.state in (AgentState.COOLDOWN, AgentState.IDLE)
    # the FSM took the no_progress transition -> counter advanced, tool_error did NOT
    assert w.fsm.no_progress_counter == 1
    # task was failed (requeued or failed) with the no_progress reason
    row = eng._conn.execute(
        "SELECT status, attempts FROM tasks WHERE type='CODEGEN'").fetchone()
    assert row is not None
    evs = [e for _, e in EventStore(eng.db_path).read_since(
        0, types=("task.failed", "task.requeued"))]
    assert evs and "no_progress" in (evs[-1].payload.get("reason") or "")
    w.close(); eng.close()
