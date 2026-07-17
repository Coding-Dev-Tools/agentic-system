"""Deterministic agent FSM with per-state tool policy.

LLMs never control flow — only named FSM events / engine methods move state.
Tool policy is attached to states so the host can gate what's available at
runtime without sprinkling ``if`` checks through agent code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from agentic_system.ports import get_config_port

# ── Tool policy ───────────────────────────────────────────────────────────

DEFAULT_STATE_TOOL_POLICY = {
    "IDLE": {"allow": "*", "deny": ()},
    "PLANNING": {"allow": ("think", "skill_view", "terminal"), "deny": ()},
    "EXECUTING": {"allow": "*", "deny": ("deploy", "push", "publish")},
    "WAITING_HUMAN": {"allow": ("think", "skill_view"), "deny": ("terminal", "deploy", "push", "publish")},
    "BLOCKED": {"allow": ("think", "skill_view"), "deny": ("terminal", "deploy", "push", "publish", "bash")},
    "DONE": {"allow": ("think", "skill_view"), "deny": ()},
    "ERRORED": {"allow": ("think", "skill_view", "terminal"), "deny": ("deploy", "push", "publish")},
}


@dataclass
class ToolPolicy:
    allow: tuple[str, ...] = ("*",)
    deny: tuple[str, ...] = ()

    def allows(self, tool: str) -> bool:
        if "*" in self.deny:
            return False
        if tool in self.deny:
            return False
        if "*" in self.allow:
            return True
        return tool in self.allow


# ── States &get_tool_policy = lambda state: ToolPolicy(
    **get_config_port().state_tool_policy().get(state, {})
) if get_config_port().state_tool_policy() else ToolPolicy(**DEFAULT_STATE_TOOL_POLICY.get(state, {"allow": "*", "deny": ()}))


# ── State machine ─────────────────────────────────────────────────────────

VALID_STATES = (
    "IDLE", "PLANNING", "EXECUTING", "WAITING_HUMAN",
    "BLOCKED", "DONE", "ERRORED",
)

VALID_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "IDLE": ("PLANNING", "EXECUTING", "ERRORED"),
    "PLANNING": ("EXECUTING", "WAITING_HUMAN", "ERRORED", "IDLE"),
    "EXECUTING": ("WAITING_HUMAN", "BLOCKED", "DONE", "ERRORED", "PLANNING"),
    "WAITING_HUMAN": ("EXECUTING", "ERRORED", "IDLE"),
    "BLOCKED": ("EXECUTING", "ERRORED", "IDLE"),
    "DONE": ("IDLE",),
    "ERRORED": ("IDLE", "PLANNING"),
}


class InvalidTransition(Exception):
    pass


@dataclass
class AgentState:
    state: str = "IDLE"
    context: dict = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    _policy_cache: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if self.state not in VALID_STATES:
            raise ValueError(f"invalid initial state {self.state!r}")

    def transition(self, next_state: str, *, reason: str = "") -> "AgentState":
        if next_state not in VALID_TRANSITIONS.get(self.state, ()):
            raise InvalidTransition(f"{self.state} -> {next_state} not allowed")
        old = self.state
        self.state = next_state
        self.history.append({
            "from": old, "to": next_state, "reason": reason,
            "ts": __import__("datetime").datetime.utcnow().isoformat() + "Z"
        })
        return self

    def can_use_tool(self, tool: str) -> bool:
        policy = self._policy_cache.get(self.state)
        if policy is None:
            policy = get_tool_policy(self.state)
            self._policy_cache[self.state] = policy
        return policy.allows(tool)


def orchestration_enabled() -> bool:
    return get_config_port().orchestration_enabled()