"""Tests for agentic-system core."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agentic_system.breakers import BreakerRegistry, get_registry, reset_registry_for_tests
from agentic_system.council.schemas import CouncilMember, CouncilRequest, CouncilThresholds, ModelReview
from agentic_system.events import EventBus, connect, ensure_state_tables
from agentic_system.no_progress import NoProgressDetector
from agentic_system.ports import (ConfigPort, CronPort, EngraphisPort, LLMFn,
                                   TokenBudgetPort, get_config_port,
                                   reset_ports_for_tests, set_config_port,
                                   set_cron_port, set_default_llm_fn,
                                   set_engraphis_port, set_token_budget_port)
from agentic_system.state_machine import AgentState, DEFAULT_STATE_TOOL_POLICY, InvalidTransition


# ── Fake ports for tests ──────────────────────────────────────────────────

class FakeConfigPort:
    def orchestration_enabled(self): return True
    def events_db_path(self): return ":memory:"
    def council_config(self): return {
        "members": [{"id": "test-model", "provider": "test", "weight": 1.0}],
        "thresholds": {"min_overall": 3.0, "min_safety": 3.0, "min_tests": 3.0,
                       "min_agreement": 0.5, "reject_max_overall": 2.0},
        "peer_eval": "never", "min_quorum": 1,
    }
    def state_tool_policy(self): return None
    def high_impact_tool_patterns(self): return ("deploy*", "*push*")

class FakeTokenBudgetPort:
    class Budget:
        def __init__(self, max_tokens): self.max_total = max_tokens; self.used = 0; self.exceeded = False
        def consume(self, tokens): self.used += tokens; self.exceeded = self.used > self.max_total
    def make(self, max_tokens): return self.Budget(max_tokens)

class FakeCronPort:
    def scripts_dir(self): return "/tmp/scripts"
    def list_job_names(self): return []
    def create_job(self, *, name, schedule, script, workdir): pass


def test_ports_reset():
    reset_ports_for_tests()
    assert get_config_port() is None  # raises RuntimeError when accessed


def test_config_port_registration():
    reset_ports_for_tests()
    set_config_port(FakeConfigPort())
    cfg = get_config_port()
    assert cfg.orchestration_enabled() is True
    assert cfg.events_db_path() == ":memory:"


def test_token_budget_port():
    reset_ports_for_tests()
    set_token_budget_port(FakeTokenBudgetPort())
    budget = get_token_budget_port().make(100)
    budget.consume(30)
    assert budget.used == 30
    assert not budget.exceeded
    budget.consume(80)
    assert budget.exceeded


def test_cron_port_registration():
    reset_ports_for_tests()
    set_cron_port(FakeCronPort())
    cron = get_cron_port()
    assert cron.scripts_dir() == "/tmp/scripts"


def test_engraphis_port_registration():
    reset_ports_for_tests()
    class FakeEngraphisPort:
        def remember(self, content, workspace, mtype, scope, title, source, kind, importance): return "mem_123"
    set_engraphis_port(FakeEngraphisPort())
    from agentic_system.ports import get_engraphis_port
    port = get_engraphis_port()
    assert port is not None
    assert port.remember("", "", "", "", "", "", "", 0.0) == "mem_123"


def test_default_llm_fn():
    reset_ports_for_tests()
    def fake_llm(member, system, user): return '{"self_scores": {"correctness": 4}, "recommendation": "approve", "rationale": "ok"}'
    set_default_llm_fn(fake_llm)
    from agentic_system.ports import get_default_llm_fn
    assert get_default_llm_fn()(None, "", "") == '{"self_scores": {"correctness": 4}, "recommendation": "approve", "rationale": "ok"}'


# ── State machine ────────────────────────────────────────────────────────

def test_agent_state_valid_transitions():
    s = AgentState()
    assert s.state == "IDLE"
    s.transition("PLANNING", reason="start")
    assert s.state == "PLANNING"
    s.transition("EXECUTING")
    assert s.state == "EXECUTING"
    s.transition("DONE")
    assert s.state == "DONE"


def test_agent_state_invalid_transition():
    s = AgentState(state="IDLE")
    with pytest.raises(InvalidTransition):
        s.transition("DONE")  # IDLE -> DONE not allowed


def test_tool_policy_allows():
    s = AgentState(state="EXECUTING")
    assert s.can_use_tool("terminal") is True
    assert s.can_use_tool("deploy") is False  # denied in EXECUTING


# ── Breakers ──────────────────────────────────────────────────────────────

def test_breaker_registry_basic():
    reset_registry_for_tests()
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        reg = BreakerRegistry(f.name)
        assert reg.state("agent", "test-agent") == "CLOSED"
        reg.open("agent", "test-agent", "too many errors")
        assert reg.is_open("agent", "test-agent")
        reg.half_open("agent", "test-agent", "probation")
        assert reg.state("agent", "test-agent") == "HALF_OPEN"
        reg.close("agent", "test-agent", "recovered")
        assert reg.state("agent", "test-agent") == "CLOSED"


def test_global_breaker_blocks_high_impact():
    reset_registry_for_tests()
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        reg = BreakerRegistry(f.name)
        reg.open("global", "system", "incident")
        assert not reg.allow_high_impact()
        assert not reg.should_accept_task()


# ── Events ────────────────────────────────────────────────────────────────

def test_event_bus_publish_subscribe():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        bus = EventBus(f.name)
        received = []
        bus.subscribe("test.event", lambda e: received.append(e))
        bus.publish("test.event", {"foo": "bar"}, aggregate_type="Test", aggregate_id="1")
        assert len(received) == 1
        assert received[0].payload == {"foo": "bar"}
        # Query
        rows = bus.query(aggregate_type="Test", aggregate_id="1")
        assert len(rows) == 1
        bus.close()


# ── Council schemas ──────────────────────────────────────────────────────

def test_council_request_hashes():
    req = CouncilRequest(
        subject_type="CODE_EDIT",
        content="diff --git a/x.py b/x.py\n+print(1)",
        rubric_dimensions=("correctness", "safety"),
        scale_min=1, scale_max=5,
    )
    h1 = req.subject_hash()
    h2 = req.rubric_hash()
    assert len(h1) == 64  # sha256 hex
    assert len(h2) == 64


def test_model_review_approves():
    r = ModelReview(model_id="m1", self_scores={"correctness": 4, "safety": 5},
                    recommendation="approve", rationale="good")
    assert r.approves is True
    assert abs(r.self_overall - 4.5) < 0.01


# ── No-progress ──────────────────────────────────────────────────────────

def test_no_progress_detector_verbatim():
    det = NoProgressDetector(window=3, threshold=0.95)
    assert not det.record("first turn")
    assert not det.record("second turn")
    assert det.record("third turn")  # identical to first would trigger, but not here
    # Actually need identical content
    det.reset()
    assert not det.record("hello")
    assert not det.record("world")
    assert det.record("hello")  # "hello" matches first (difflib ratio = 1.0)


# ── Workflow ─────────────────────────────────────────────────────────────

def test_workflow_def_topological_order():
    from agentic_system.workflow.definitions import TaskDef, WorkflowDef
    wf = WorkflowDef("test", tasks=(
        TaskDef("a", outputs=("x",)),
        TaskDef("b", inputs=("x",), outputs=("y",)),
        TaskDef("c", inputs=("y",), outputs=("z",)),
    ))
    order = wf.topological_order()
    assert [t.name for t in order] == ["a", "b", "c"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])