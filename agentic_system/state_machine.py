"""Explicit agent finite state machine (handoff §3.1).

Formalizes the states that were implicit in the conversation loop
(TurnRetryState = RETRYING bookkeeping, IterationBudget/TokenBudget =
EXECUTING guards, retry_utils recovery guards = transition triggers) into a
deterministic, event-emitting FSM.

Invariant (the single most important one in the handoff): **LLMs never
control flow.** Model output is data; only named events drive transitions,
and an event not present in the transition table raises
:class:`InvalidTransition` instead of silently doing something.

Every successful transition is emitted to the orchestration event bus as
``agent.state_changed`` so the event store is the system of record for agent
lifecycle. Workflow workers (phase 3) drive this machine; the interactive
conversation loop only reports lifecycle events and budgets.
"""

from __future__ import annotations

import fnmatch
import time
from enum import Enum
from typing import Any, Callable, Optional


class AgentState(str, Enum):
    IDLE = "IDLE"
    CLAIM_TASK = "CLAIM_TASK"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    WAITING_DEP = "WAITING_DEP"
    REVIEWING = "REVIEWING"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    COOLDOWN = "COOLDOWN"


class InvalidTransition(Exception):
    def __init__(self, state: AgentState, event: str):
        self.state, self.event = state, event
        super().__init__(f"event {event!r} is not valid in state {state.value}")


# (current_state, event) -> next_state — handoff §1.2 transition table,
# plus claim_lost (CAS race), dependency_failed, budget/no-progress exits.
TRANSITIONS: dict[tuple[AgentState, str], AgentState] = {
    (AgentState.IDLE, "task_available"): AgentState.CLAIM_TASK,
    (AgentState.CLAIM_TASK, "task_claimed"): AgentState.PLANNING,
    (AgentState.CLAIM_TASK, "claim_lost"): AgentState.IDLE,
    (AgentState.PLANNING, "plan_ok"): AgentState.EXECUTING,
    (AgentState.PLANNING, "planning_error"): AgentState.FAILED,
    (AgentState.EXECUTING, "output_ready"): AgentState.REVIEWING,
    (AgentState.EXECUTING, "waiting_on_dependency"): AgentState.WAITING_DEP,
    (AgentState.EXECUTING, "tool_error"): AgentState.FAILED,
    (AgentState.EXECUTING, "no_progress"): AgentState.FAILED,
    (AgentState.EXECUTING, "budget_exhausted"): AgentState.FAILED,
    (AgentState.WAITING_DEP, "deps_resolved"): AgentState.EXECUTING,
    (AgentState.WAITING_DEP, "dependency_failed"): AgentState.FAILED,
    (AgentState.REVIEWING, "approved"): AgentState.COMPLETED,
    (AgentState.REVIEWING, "needs_changes"): AgentState.RETRYING,
    (AgentState.RETRYING, "retry_allowed"): AgentState.EXECUTING,
    (AgentState.RETRYING, "retries_exhausted"): AgentState.FAILED,
    (AgentState.FAILED, "soft_fail"): AgentState.COOLDOWN,
    (AgentState.COOLDOWN, "cooldown_elapsed"): AgentState.IDLE,
    (AgentState.COMPLETED, "reset"): AgentState.IDLE,
}

_ERROR_EVENTS = {"planning_error", "tool_error", "retries_exhausted",
                 "dependency_failed", "budget_exhausted"}


class AgentStateMachine:
    """One instance per logical agent (workflow worker), not per process."""

    def __init__(
        self,
        agent_id: str,
        role: str = "",
        bus: Any = None,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ):
        self.agent_id = agent_id
        self.role = role
        self.bus = bus
        self.cooldown_seconds = float(cooldown_seconds)
        self.clock = clock
        self.state: AgentState = AgentState.IDLE
        self.current_task_id: Optional[str] = None
        self.error_count = 0
        self.no_progress_counter = 0
        self.next_eligible_at: Optional[float] = None
        self.last_event: Optional[str] = None
        self.last_transition_at: float = clock()

    # ── core ─────────────────────────────────────────────────────────────
    def handle(self, event: str, payload: Optional[dict[str, Any]] = None) -> AgentState:
        """Apply ``event``; returns the new state or raises InvalidTransition."""
        key = (self.state, event)
        if key not in TRANSITIONS:
            raise InvalidTransition(self.state, event)
        prev = self.state
        self.state = TRANSITIONS[key]
        self.last_event = event
        self.last_transition_at = self.clock()

        if event in _ERROR_EVENTS:
            self.error_count += 1
        if event == "no_progress":
            self.no_progress_counter += 1
        if event == "task_claimed":
            self.current_task_id = (payload or {}).get("task_id")
        if self.state == AgentState.COOLDOWN:
            self.next_eligible_at = self.clock() + self.cooldown_seconds
        if self.state == AgentState.IDLE:
            self.current_task_id = None
            self.next_eligible_at = None

        self._emit(prev, event, payload)
        return self.state

    def tick(self) -> AgentState:
        """Clock-driven housekeeping: auto-leave COOLDOWN once elapsed."""
        if (
            self.state == AgentState.COOLDOWN
            and self.next_eligible_at is not None
            and self.clock() >= self.next_eligible_at
        ):
            return self.handle("cooldown_elapsed")
        return self.state

    def can_accept_task(self) -> bool:
        self.tick()
        return self.state == AgentState.IDLE

    def _emit(self, prev: AgentState, event: str, payload: Optional[dict]) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(
                "agent.state_changed",
                {
                    "agent_id": self.agent_id,
                    "role": self.role,
                    "from": prev.value,
                    "to": self.state.value,
                    "event": event,
                    "task_id": self.current_task_id,
                    "error_count": self.error_count,
                    "no_progress_counter": self.no_progress_counter,
                    "data": payload or {},
                },
                aggregate_type="Agent",
                aggregate_id=self.agent_id,
                correlation_id=self.current_task_id,
            )
        except Exception:  # event emission must never break the FSM
            pass

    def to_dict(self) -> dict[str, Any]:
        """Snapshot for the agent_instances table / dashboard."""
        return {
            "id": self.agent_id,
            "role": self.role,
            "status": self.state.value,
            "current_task_id": self.current_task_id,
            "error_count": self.error_count,
            "no_progress_counter": self.no_progress_counter,
            "next_eligible_at": self.next_eligible_at,
            "last_event": self.last_event,
            "last_transition_at": self.last_transition_at,
        }


# ── Per-state tool policy (cheapest hallucination guard, handoff §3.1) ────
#
# Config override lives under ``orchestration.state_allowed_tools`` in
# config.yaml, keyed by state name, values ``{"allow": [...], "deny": [...]}``
# (fnmatch patterns). ``allow`` of None/absent means "everything not denied".
# High-impact tools are denied outside EXECUTING by default.

_HIGH_IMPACT = ["deploy*", "*git_push*", "*push_to_remote*", "publish*",
                "release*", "*prod*deploy*"]

DEFAULT_STATE_TOOL_POLICY: dict[str, dict[str, Optional[list[str]]]] = {
    AgentState.IDLE.value: {"deny": list(_HIGH_IMPACT)},
    AgentState.CLAIM_TASK.value: {"deny": list(_HIGH_IMPACT)},
    AgentState.PLANNING.value: {"deny": list(_HIGH_IMPACT)},
    AgentState.WAITING_DEP.value: {"deny": list(_HIGH_IMPACT)},
    AgentState.REVIEWING.value: {"deny": list(_HIGH_IMPACT)},
    AgentState.RETRYING.value: {"deny": list(_HIGH_IMPACT)},
    AgentState.COOLDOWN.value: {"deny": ["*"]},  # cooling agents do nothing
    # EXECUTING / COMPLETED / FAILED: no default restrictions.
}


def is_tool_allowed(
    state: "AgentState | str",
    tool_name: str,
    policy: Optional[dict] = None,
) -> bool:
    state_key = state.value if isinstance(state, AgentState) else str(state)
    merged = dict(DEFAULT_STATE_TOOL_POLICY)
    if policy:
        merged.update(policy)
    rules = merged.get(state_key)
    if not rules:
        return True
    allow = rules.get("allow")
    if allow is not None and not any(fnmatch.fnmatch(tool_name, p) for p in allow):
        return False
    deny = rules.get("deny") or []
    return not any(fnmatch.fnmatch(tool_name, p) for p in deny)


def filter_tools(
    state: "AgentState | str",
    tool_names: list[str],
    policy: Optional[dict] = None,
) -> list[str]:
    return [t for t in tool_names if is_tool_allowed(state, t, policy)]


__all__ = [
    "AgentState", "AgentStateMachine", "InvalidTransition", "TRANSITIONS",
    "DEFAULT_STATE_TOOL_POLICY", "is_tool_allowed", "filter_tools",
]
