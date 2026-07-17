"""Workflow DAG definitions (from Hermes, generalized).

A Workflow is a static DAG of tasks with typed inputs/outputs.
Tasks are claimed by workers (CAS), executed idempotently, and advance
the workflow. Supports restart-resume (claims + idempotency keys).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from agentic_system.events import connect, ensure_state_tables, now_iso
from agentic_system.ports import get_config_port

# ── Task & Workflow definitions ───────────────────────────────────────────

@dataclass(frozen=True)
class TaskDef:
    name: str
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    # Optional: custom executor for host-specific logic
    executor: Optional[str] = None
    # Idempotency key template (can reference inputs)
    idempotency_key: Optional[str] = None


@dataclass(frozen=True)
class WorkflowDef:
    name: str
    tasks: tuple[TaskDef, ...]
    # Entry points: tasks with no inputs or inputs satisfied by initial payload
    # Computed from graph structure, but can be overridden


# ── Instance state (persisted) ────────────────────────────────────────────

@dataclass
class WorkflowInstance:
    instance_id: str
    workflow_name: str
    state: str  # PENDING | RUNNING | WAITING | DONE | FAILED
    payload: dict[str, Any]
    claimed_by: Optional[str] = None
    claim_ts: Optional[str] = None
    version: int = 1
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)


# ── Engine ────────────────────────────────────────────────────────────────

class WorkflowEngine:
    def __init__(self, db_path: Optional[str] = None):
        self._conn = connect(db_path or get_config_port().events_db_path())
        ensure_state_tables(self._conn)
        self._defs: dict[str, WorkflowDef] = {}

    def register(self, wf: WorkflowDef) -> None:
        self._defs[wf.name] = wf

    def start(self, workflow_name: str, payload: dict) -> WorkflowInstance:
        wf = self._defs.get(workflow_name)
        if not wf:
            raise ValueError(f"unknown workflow {workflow_name!r}")
        instance_id = f"{workflow_name}-{__import__('uuid').uuid4().hex[:8]}"
        inst = WorkflowInstance(
            instance_id=instance_id, workflow_name=workflow_name,
            state="PENDING", payload=payload
        )
        self._conn.execute(
            """INSERT INTO workflow_instances
               (instance_id, workflow_name, state, payload_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?)""",
            (inst.instance_id, inst.workflow_name, inst.state,
             json.dumps(inst.payload), inst.created_at, inst.updated_at))
        self._conn.commit()
        return inst

    def claim_task(self, instance_id: str, worker_id: str) -> Optional[TaskDef]:
        """CAS claim the next runnable task. Returns TaskDef or None."""
        inst = self._load_instance(instance_id)
        if not inst or inst.state not in ("PENDING", "RUNNING", "WAITING"):
            return None
        wf = self._defs[inst.workflow_name]
        # Find next task whose inputs are satisfied
        for task in wf.tasks:
            if self._is_done(inst, task):
                continue
            if not self._inputs_ready(inst, task):
                continue
            # Try CAS claim
            key = f"{instance_id}:{task.name}"
            cur = self._conn.execute(
                "SELECT claimed_by FROM workflow_claims WHERE instance_id=? AND task_name=?",
                (instance_id, task.name)).fetchone()
            if cur and cur["claimed_by"]:
                continue  # already claimed
            # Insert or update claim
            ts = now_iso()
            self._conn.execute(
                """INSERT INTO workflow_claims (instance_id, task_name, claimed_by, claim_ts)
                   VALUES (?,?,?,?)
                   ON CONFLICT(instance_id, task_name) DO UPDATE SET
                     claimed_by=excluded.claimed_by, claim_ts=excluded.claim_ts""",
                (instance_id, task.name, worker_id, ts))
            self._conn.execute(
                "UPDATE workflow_instances SET state='RUNNING', claimed_by=?, claim_ts=?, "
                "version=version+1, updated_at=? WHERE instance_id=?",
                (worker_id, ts, ts, instance_id))
            self._conn.commit()
            return task
        return None

    def complete_task(self, instance_id: str, task_name: str,
                      worker_id: str, outputs: dict, success: bool = True) -> bool:
        """Mark task complete (idempotent). Advances workflow."""
        inst = self._load_instance(instance_id)
        if not inst:
            return False
        # Verify claim
        cur = self._conn.execute(
            "SELECT claimed_by FROM workflow_claims WHERE instance_id=? AND task_name=?",
            (instance_id, task_name)).fetchone()
        if not cur or cur["claimed_by"] != worker_id:
            return False
        # Record outcome
        self._conn.execute(
            """UPDATE workflow_claims SET outcome=?, result_json=? WHERE instance_id=? AND task_name=?""",
            ("SUCCESS" if success else "FAILURE", json.dumps(outputs), instance_id, task_name))
        if not success:
            self._conn.execute(
                "UPDATE workflow_instances SET state='FAILED', updated_at=? WHERE instance_id=?",
                (now_iso(), instance_id))
            self._conn.commit()
            return False
        # Check if workflow complete
        wf = self._defs[inst.workflow_name]
        all_done = all(self._is_done(inst, t) for t in wf.tasks)
        if all_done:
            self._conn.execute(
                "UPDATE workflow_instances SET state='DONE', updated_at=? WHERE instance_id=?",
                (now_iso(), instance_id))
        else:
            self._conn.execute(
                "UPDATE workflow_instances SET state='RUNNING', updated_at=? WHERE instance_id=?",
                (now_iso(), instance_id))
        self._conn.commit()
        return True

    def _load_instance(self, instance_id: str) -> Optional[WorkflowInstance]:
        row = self._conn.execute(
            "SELECT * FROM workflow_instances WHERE instance_id=?", (instance_id,)).fetchone()
        if not row:
            return None
        return WorkflowInstance(
            instance_id=row["instance_id"], workflow_name=row["workflow_name"],
            state=row["state"], payload=json.loads(row["payload_json"]),
            claimed_by=row["claimed_by"], claim_ts=row["claim_ts"],
            version=row["version"], created_at=row["created_at"], updated_at=row["updated_at"])

    def _is_done(self, inst: WorkflowInstance, task: TaskDef) -> bool:
        row = self._conn.execute(
            "SELECT outcome FROM workflow_claims WHERE instance_id=? AND task_name=?",
            (inst.instance_id, task.name)).fetchone()
        return row and row["outcome"] == "SUCCESS"

    def _inputs_ready(self, inst: WorkflowInstance, task: TaskDef) -> bool:
        for inp in task.inputs:
            if inp not in inst.payload:
                # Check if produced by another task
                produced = False
                for t in self._defs[inst.workflow_name].tasks:
                    if inp in t.outputs and self._is_done(inst, t):
                        produced = True
                        break
                if not produced:
                    return False
        return True

    def close(self) -> None:
        self._conn.close()


import json