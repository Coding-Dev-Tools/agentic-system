"""Generic workflow worker: claims tasks, drives the agent FSM, runs handlers.

The deterministic seam between the workflow engine and probabilistic work:
handlers do the actual work (an AIAgent turn, a subprocess test run, a council
session) and return an ``output_ref`` (ideally an Engraphis memory id). The
worker owns all control flow — FSM transitions, heartbeats, per-state tool
policy, budgets -- so LLM output can never steer the DAG.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from agentic_system.events.state_tables import connect, ensure_state_tables, heartbeat
from agentic_system.no_progress import NoProgress, NoProgressDetector
from agentic_system.state_machine import AgentState, AgentStateMachine
from agentic_system.workflow.engine import WorkflowEngine

logger = logging.getLogger("agentic_system.workflow.worker")

# handler(task_dict) -> output_ref | None; raise to fail the task
Handler = Callable[[dict], Optional[str]]


class WorkflowWorker:
    def __init__(self, agent_id: str, engine: WorkflowEngine,
                 handlers: dict[str, Handler], role: str = "",
                 cooldown_seconds: float = 60.0,
                 no_progress: Optional[NoProgressDetector] = None):
        self.agent_id = agent_id
        self.engine = engine
        self.handlers = handlers
        self.role = role
        self.fsm = AgentStateMachine(agent_id, role=role, bus=engine.bus,
                                     cooldown_seconds=cooldown_seconds)
        self._conn = connect(engine.db_path)
        ensure_state_tables(self._conn)
        # Optional loop detector a handler raises out of; the worker maps the
        # raised NoProgress onto the FSM ``no_progress`` event (EXECUTING -> FAILED).
        # The handler owns the per-step observe() calls (it has the tool outputs);
        # the worker owns the control-flow bridge. None disables the bridge.
        self.no_progress = no_progress

    def run_once(self) -> bool:
        """Claim and execute at most one task. Returns True if work was done."""
        heartbeat(self._conn, self.agent_id, self.role,
                  status=self.fsm.state.value)
        try:
            if not self.fsm.can_accept_task():
                return False
            task = None
            self.fsm.handle("task_available")  # optimistic; claim may still lose
            task = self.engine.claim_next(self.agent_id,
                                          types=list(self.handlers), role=self.role or None)
            if task is None:
                self.fsm.handle("claim_lost")
                return False
            self.fsm.handle("task_claimed", {"task_id": task["id"]})
            heartbeat(self._conn, self.agent_id, self.role, status="EXECUTING")
            handler = self.handlers[task["type"]]
            self.fsm.handle("plan_ok")  # handlers embed their own planning
            output_ref = handler(task)
            self.fsm.handle("output_ready")
            self.engine.complete_task(task["id"], output_ref=output_ref)
            self.fsm.handle("approved")
            self.fsm.handle("reset")
            if self.no_progress is not None:
                self.no_progress.reset()  # fresh window for the next task
            return True
        except Exception as exc:
            task_id = task["id"] if task is not None else None
            reason = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, NoProgress):
                reason = f"no_progress: {exc}"
            if task_id is not None:
                logger.exception("worker %s failed task %s", self.agent_id, task_id)
                self.engine.fail_task(task_id, reason=reason)
            else:
                # failed before a task was claimed (e.g. claim_next raised) --
                # nothing to requeue; just log and cool down.
                logger.exception("worker %s failed before claiming a task", self.agent_id)
            # A NoProgress loop drives the dedicated FSM event (EXECUTING -> FAILED);
            # any other failure -> tool_error/planning_error (also -> FAILED).
            if self.fsm.state == AgentState.EXECUTING:
                self.fsm.handle("no_progress" if isinstance(exc, NoProgress) else "tool_error")
            elif self.fsm.state == AgentState.PLANNING:
                self.fsm.handle("planning_error")
            self.fsm.handle("soft_fail")  # -> COOLDOWN; tick() releases it
            return True
        finally:
            # materialize the terminal FSM state (IDLE after reset/claim_lost,
            # COOLDOWN after a failure) so agent_instances never reports a
            # stale EXECUTING for an idle agent — heartbeat_sweep and the
            # status CLI rely on this being accurate.
            heartbeat(self._conn, self.agent_id, self.role,
                      status=self.fsm.state.value)

    def close(self) -> None:
        self._conn.close()


__all__ = ["WorkflowWorker"]
