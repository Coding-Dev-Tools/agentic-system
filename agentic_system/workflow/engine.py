"""Minimal DAG executor over the shared state tables.

Not Temporal/Airflow: one SQLite DB, CAS task claiming, event emission for
every lifecycle change. The engine keeps NO in-memory state — runs, tasks and
node readiness are always derived from the tables, which is what makes a worker
process restart a non-event: construct a
new engine on the same DB and call ``advance(run_id)`` (or just keep claiming
tasks) to resume from where the previous process died.

Write-lock discipline (learned in phase 2): mutate + commit first, publish
events after — a second connection writing mid-transaction deadlocks SQLite.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from agentic_system.breakers import BreakerRegistry
from agentic_system.events.bus import EventBus
from agentic_system.events.state_tables import connect, ensure_state_tables, now_iso
from agentic_system.events.store import EventStore
from agentic_system.workflow.definitions import WorkflowDef, load_directory

_TERMINAL_OK = "DONE"
_TERMINAL_BAD = "FAILED"


class WorkflowEngine:
    def __init__(self, db_path: str, definitions: Optional[dict[str, WorkflowDef]] = None,
                 bus: Optional[EventBus] = None, breakers: Optional[BreakerRegistry] = None):
        self.db_path = db_path
        self._conn = connect(db_path)
        ensure_state_tables(self._conn)
        self.definitions = definitions if definitions is not None else load_directory()
        self._own_store = None
        if bus is None:
            self._own_store = EventStore(db_path)
            bus = EventBus(self._own_store, source="workflow-engine")
        self.bus = bus
        self.breakers = breakers

    # ── run lifecycle ────────────────────────────────────────────────────
    def start_run(self, workflow_name: str, context: Optional[dict[str, Any]] = None) -> str:
        wf = self._def(workflow_name)
        if self.breakers is not None and not self.breakers.should_accept_task(workflow=workflow_name):
            raise RuntimeError(f"workflow breaker open for {workflow_name!r}; refusing to start run")
        run_id = f"wfr-{uuid.uuid4()}"
        ts = now_iso()
        self._conn.execute(
            "INSERT INTO workflow_runs (id, workflow_name, status, context_json, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (run_id, wf.name, "RUNNING", json.dumps(context or {}), ts, ts),
        )
        self._conn.commit()
        self.bus.publish("workflow.run_started",
                         {"run_id": run_id, "workflow": wf.name, "context": context or {}},
                         aggregate_type="WorkflowRun", aggregate_id=run_id,
                         correlation_id=run_id)
        self.advance(run_id)
        return run_id

    def advance(self, run_id: str) -> list[str]:
        """Create tasks for every node whose dependencies are all DONE and
        which has no task yet. Also settles the run when terminal.
        Idempotent — safe to call any number of times, from any process."""
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown run {run_id}")
        if run["status"] not in ("RUNNING",):
            return []
        wf = self._def(run["workflow_name"])
        tasks = {t["node_id"]: t for t in self.list_tasks(run_id)}

        created: list[str] = []
        pending_events = []
        for nid in wf.topo_order():
            node = wf.nodes[nid]
            if nid in tasks:
                continue
            if all(tasks.get(dep, {}).get("status") == _TERMINAL_OK for dep in node.depends_on):
                task_id = f"task-{uuid.uuid4()}"
                ts = now_iso()
                self._conn.execute(
                    """INSERT INTO tasks (id, type, status, workflow_run_id, node_id,
                           input_ref, dependencies_json, max_attempts, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (task_id, node.task_type, "PENDING", run_id, nid,
                     json.dumps({"params": node.params,
                                 "required_role": node.required_role,
                                 "context": json.loads(run["context_json"])}),
                     json.dumps(list(node.depends_on)), node.max_attempts, ts, ts),
                )
                created.append(task_id)
                pending_events.append((
                    "workflow.node_ready",
                    {"run_id": run_id, "node_id": nid, "task_id": task_id,
                     "task_type": node.task_type},
                    dict(aggregate_type="WorkflowRun", aggregate_id=run_id,
                         correlation_id=run_id),
                ))
                pending_events.append((
                    "task.created",
                    {"task_id": task_id, "task_type": node.task_type,
                     "required_role": node.required_role or "", "node_id": nid},
                    dict(aggregate_type="Task", aggregate_id=task_id,
                         correlation_id=run_id),
                ))
                tasks[nid] = {"status": "PENDING"}

        # settle run status + execution cursor
        statuses: dict[str, str] = {nid: tasks.get(nid, {}).get("status", "PENDING") for nid in wf.nodes}
        frontier = next((nid for nid in wf.topo_order()
                         if statuses[nid] not in (_TERMINAL_OK,)), None)
        if any(s == _TERMINAL_BAD for s in statuses.values()):
            new_status, evt = "FAILED", "workflow.run_failed"
        elif all(s == _TERMINAL_OK for s in statuses.values()):
            new_status, evt = "COMPLETED", "workflow.run_completed"
        else:
            new_status, evt = "RUNNING", None
        self._conn.execute(
            "UPDATE workflow_runs SET status=?, current_node_id=?, updated_at=? WHERE id=?",
            (new_status, frontier, now_iso(), run_id),
        )
        self._conn.commit()
        for etype, payload, kw in pending_events:
            self.bus.publish(etype, payload, **kw)  # type: ignore[arg-type]
        if evt:
            self.bus.publish(evt, {"run_id": run_id, "workflow": run["workflow_name"]},
                             aggregate_type="WorkflowRun", aggregate_id=run_id,
                             correlation_id=run_id,
                             priority="high" if evt.endswith("failed") else "normal")
        return created

    # ── task lifecycle (called by workers) ───────────────────────────────
    def claim_task(self, task_id: str, agent_id: str) -> bool:
        """Atomic CAS claim; also refuses when a breaker is open."""
        task = self.get_task(task_id)
        if task is None:
            return False
        if self.breakers is not None and not self.breakers.should_accept_task(
                agent_id=agent_id, workflow=self._run_workflow(task["workflow_run_id"])):
            return False
        cur = self._conn.execute(
            """UPDATE tasks SET status='ASSIGNED', assigned_agent_id=?, updated_at=?
               WHERE id=? AND status='PENDING'""",
            (agent_id, now_iso(), task_id),
        )
        self._conn.commit()
        if not cur.rowcount:
            return False
        self.bus.publish("task.claimed", {"task_id": task_id, "agent_id": agent_id},
                         aggregate_type="Task", aggregate_id=task_id,
                         correlation_id=task["workflow_run_id"])
        return True

    def claim_next(self, agent_id: str, types: Optional[list[str]] = None,
                   role: Optional[str] = None) -> Optional[dict]:
        """Claim the oldest matching PENDING task (agents race via CAS)."""
        q = "SELECT * FROM tasks WHERE status='PENDING'"
        args: list[Any] = []
        if types:
            q += " AND type IN (%s)" % ",".join("?" * len(types))
            args.extend(types)
        q += " ORDER BY created_at ASC"
        for row in self._conn.execute(q, args).fetchall():
            task = dict(row)
            if role is not None:
                required = (json.loads(task["input_ref"] or "{}")).get("required_role")
                if required and required != role:
                    continue
            if self.claim_task(task["id"], agent_id):
                return self.get_task(task["id"])
        return None

    def complete_task(self, task_id: str, output_ref: Optional[str] = None) -> None:
        task = self._require_assigned(task_id)
        self._conn.execute(
            "UPDATE tasks SET status='DONE', output_ref=?, updated_at=? WHERE id=?",
            (output_ref, now_iso(), task_id),
        )
        self._conn.commit()
        self.bus.publish("task.completed",
                         {"task_id": task_id, "node_id": task["node_id"],
                          "output_ref": output_ref},
                         aggregate_type="Task", aggregate_id=task_id,
                         correlation_id=task["workflow_run_id"])
        if task["workflow_run_id"]:
            self.advance(task["workflow_run_id"])

    def fail_task(self, task_id: str, reason: str) -> str:
        """Returns 'requeued' or 'failed' (retries exhausted -> escalation)."""
        task = self._require_assigned(task_id)
        attempts = task["attempts"] + 1
        exhausted = attempts >= task["max_attempts"]
        self._conn.execute(
            """UPDATE tasks SET status=?, assigned_agent_id=NULL, attempts=?,
                   updated_at=? WHERE id=?""",
            ("FAILED" if exhausted else "PENDING", attempts, now_iso(), task_id),
        )
        self._conn.commit()
        if exhausted:
            self.bus.publish("task.failed",
                             {"task_id": task_id, "reason": reason, "attempts": attempts},
                             aggregate_type="Task", aggregate_id=task_id,
                             correlation_id=task["workflow_run_id"], priority="high")
            # escalation hook -- the council consumes this
            self.bus.publish("council.escalation_requested",
                             {"task_id": task_id, "node_id": task["node_id"],
                              "reason": f"retries_exhausted: {reason}"},
                             aggregate_type="Task", aggregate_id=task_id,
                             correlation_id=task["workflow_run_id"], priority="high")
            if task["workflow_run_id"]:
                self.advance(task["workflow_run_id"])
            return "failed"
        self.bus.publish("task.requeued",
                         {"task_id": task_id, "reason": reason, "attempts": attempts},
                         aggregate_type="Task", aggregate_id=task_id,
                         correlation_id=task["workflow_run_id"])
        return "requeued"

    # ── queries ──────────────────────────────────────────────────────────
    def get_run(self, run_id: str) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def get_task(self, task_id: str) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, run_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE workflow_run_id=? ORDER BY created_at ASC",
            (run_id,)).fetchall()
        return [dict(r) for r in rows]

    # ── internals ────────────────────────────────────────────────────────
    def _def(self, name: str) -> WorkflowDef:
        if name not in self.definitions:
            raise KeyError(f"unknown workflow {name!r}; known: {sorted(self.definitions)}")
        return self.definitions[name]

    def _run_workflow(self, run_id: Optional[str]) -> Optional[str]:
        if not run_id:
            return None
        run = self.get_run(run_id)
        return run["workflow_name"] if run else None

    def _require_assigned(self, task_id: str) -> dict:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(f"unknown task {task_id}")
        if task["status"] != "ASSIGNED":
            raise RuntimeError(f"task {task_id} is {task['status']}, not ASSIGNED")
        return task

    def close(self) -> None:
        self._conn.close()
        if self._own_store is not None:
            self._own_store.close()


__all__ = ["WorkflowEngine"]
