"""Phase 1 tests: FSM transitions, budgets, no-progress, tool policy."""

import pytest

from agentic_system.no_progress import NoProgressDetector
from agentic_system.state_machine import (
    AgentState,
    AgentStateMachine,
    InvalidTransition,
    TRANSITIONS,
    filter_tools,
    is_tool_allowed,
)


class FakeBus:
    def __init__(self):
        self.events = []

    def publish(self, type, payload=None, **kw):
        self.events.append((type, payload or {}, kw))


# ── state machine ─────────────────────────────────────────────────────────

def test_happy_path_full_cycle():
    m = AgentStateMachine("a1")
    seq = ["task_available", "task_claimed", "plan_ok", "output_ready",
           "approved", "reset"]
    for ev in seq:
        m.handle(ev)
    assert m.state == AgentState.IDLE
    assert m.error_count == 0


def test_invalid_transition_raises():
    m = AgentStateMachine("a1")
    with pytest.raises(InvalidTransition):
        m.handle("approved")
    assert m.state == AgentState.IDLE  # unchanged


def test_retry_path_and_exhaustion():
    m = AgentStateMachine("a1")
    for ev in ["task_available", "task_claimed", "plan_ok", "output_ready",
               "needs_changes", "retry_allowed", "output_ready",
               "needs_changes", "retries_exhausted"]:
        m.handle(ev)
    assert m.state == AgentState.FAILED
    assert m.error_count == 1


def test_cooldown_elapses_via_tick():
    t = {"now": 100.0}
    m = AgentStateMachine("a1", cooldown_seconds=30, clock=lambda: t["now"])
    for ev in ["task_available", "task_claimed", "planning_error", "soft_fail"]:
        m.handle(ev)
    assert m.state == AgentState.COOLDOWN
    assert m.next_eligible_at == 130.0
    assert not m.can_accept_task()
    t["now"] = 131.0
    assert m.can_accept_task()
    assert m.state == AgentState.IDLE


def test_waiting_dep_roundtrip():
    m = AgentStateMachine("a1")
    for ev in ["task_available", "task_claimed", "plan_ok",
               "waiting_on_dependency", "deps_resolved", "output_ready",
               "approved"]:
        m.handle(ev)
    assert m.state == AgentState.COMPLETED


def test_claim_lost_returns_to_idle():
    m = AgentStateMachine("a1")
    m.handle("task_available")
    m.handle("claim_lost")
    assert m.state == AgentState.IDLE


def test_no_progress_counter_and_task_binding():
    m = AgentStateMachine("a1")
    m.handle("task_available")
    m.handle("task_claimed", {"task_id": "t-9"})
    assert m.current_task_id == "t-9"
    m.handle("plan_ok")
    m.handle("no_progress")
    assert m.state == AgentState.FAILED
    assert m.no_progress_counter == 1 and m.error_count == 0


def test_transitions_emitted_to_bus():
    bus = FakeBus()
    m = AgentStateMachine("a1", role="coder", bus=bus)
    m.handle("task_available")
    m.handle("task_claimed", {"task_id": "t1"})
    assert len(bus.events) == 2
    t, payload, kw = bus.events[1]
    assert t == "agent.state_changed"
    assert payload["from"] == "CLAIM_TASK" and payload["to"] == "PLANNING"
    assert kw["aggregate_type"] == "Agent"


def test_every_transition_target_is_reachable_state():
    for (state, _), target in TRANSITIONS.items():
        assert isinstance(state, AgentState) and isinstance(target, AgentState)


def test_snapshot_shape():
    m = AgentStateMachine("a1", role="tester")
    d = m.to_dict()
    assert d["id"] == "a1" and d["status"] == "IDLE" and d["role"] == "tester"


# ── tool policy ───────────────────────────────────────────────────────────

def test_high_impact_denied_outside_executing():
    assert not is_tool_allowed("PLANNING", "deploy_prod")
    assert not is_tool_allowed("REVIEWING", "git_push_main")
    assert is_tool_allowed("EXECUTING", "deploy_prod")
    assert is_tool_allowed("PLANNING", "read_file")


def test_cooldown_denies_everything():
    assert filter_tools("COOLDOWN", ["read_file", "ls", "deploy"]) == []


def test_config_policy_overrides_default():
    policy = {"PLANNING": {"allow": ["read_*"], "deny": []}}
    assert is_tool_allowed("PLANNING", "read_file", policy)
    assert not is_tool_allowed("PLANNING", "write_file", policy)


# ── no-progress detector ──────────────────────────────────────────────────

def test_detects_verbatim_repetition():
    d = NoProgressDetector(window=3)
    assert not d.observe("ls -la output: 3 files")
    assert not d.observe("ls -la output: 3 files")
    assert d.observe("ls -la output: 3 files")
    assert d.trip_count == 1


def test_progress_resets_window():
    d = NoProgressDetector(window=3)
    d.observe("same")
    d.observe("same")
    assert not d.observe("same", state_changed=True)
    assert not d.observe("same")  # window only has 2 again


def test_different_outputs_do_not_trip():
    d = NoProgressDetector(window=3)
    assert not d.observe("step one: reading config")
    assert not d.observe("step two: patched file, 40 insertions")
    assert not d.observe("step three: tests passed, 19 green")


def test_whitespace_normalization():
    d = NoProgressDetector(window=2, threshold=0.99)
    d.observe("result:   42\n")
    assert d.observe("result: 42")


# ── loop hook wiring (static) ─────────────────────────────────────────────

def test_conversation_loop_hooks_are_guarded():
    # Hermes-specific integration check removed for the framework-agnostic
    # package (the conversation_loop wiring lives in the Hermes host).
    pass
