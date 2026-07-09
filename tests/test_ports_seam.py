"""Seam test: the core routes config / token-budget / LLM / cron through
swappable adapter ports (agentic_system.ports). A host registers its own; the
core has no host imports.
"""

import pytest

from agentic_system import ports
from agentic_system.events import hooks as orch_hooks


class _FakeConfigPort:
    def __init__(self, enabled=True, db="/tmp/fake.db", council=None):
        self._enabled, self._db, self._council = enabled, db, council
        self.calls = []

    def orchestration_enabled(self):
        self.calls.append("enabled")
        return self._enabled

    def events_db_path(self):
        self.calls.append("db")
        return self._db

    def council_config(self):
        return self._council

    def state_tool_policy(self):
        return None


class _FakeBudget:
    def __init__(self, max_tokens):
        self.max_total = max_tokens
        self.used = 0
        self.exceeded = False
        self.consumed = []

    def consume(self, tokens):
        self.consumed.append(tokens)
        self.used += tokens
        self.exceeded = self.used >= self.max_total


class _FakeTokenBudgetPort:
    def __init__(self):
        self.made = []

    def make(self, max_tokens):
        b = _FakeBudget(max_tokens)
        self.made.append(b)
        return b


class _FakeCronPort:
    def __init__(self, scripts_dir="/tmp/scripts"):
        self._scripts_dir = scripts_dir
        self.names = []
        self.created = []

    def scripts_dir(self):
        return self._scripts_dir

    def list_job_names(self):
        return list(self.names)

    def create_job(self, *, name, schedule, script, workdir):
        self.created.append((name, schedule, script, workdir))


@pytest.fixture()
def restore():
    yield
    ports.reset_ports_for_tests()
    orch_hooks.reset_bus_for_tests()


def test_hooks_delegate_to_registered_config_port(restore):
    fake = _FakeConfigPort(enabled=True, db="/tmp/x.db")
    ports.set_config_port(fake)
    assert orch_hooks.orchestration_enabled() is True
    assert orch_hooks.events_db_path() == "/tmp/x.db"
    assert "enabled" in fake.calls and "db" in fake.calls


def test_disabled_port_short_circuits_bus(restore):
    ports.set_config_port(_FakeConfigPort(enabled=False))
    orch_hooks.reset_bus_for_tests()
    assert orch_hooks.get_bus() is None


def test_attach_token_budget_uses_registered_port(restore):
    ports.set_config_port(_FakeConfigPort(enabled=True, db="/tmp/x.db"))
    bp = _FakeTokenBudgetPort()
    ports.set_token_budget_port(bp)

    class A:
        pass
    a = A()
    orch_hooks.attach_token_budget(a, 1000)
    assert isinstance(a.token_budget, _FakeBudget)
    assert a.token_budget.max_total == 1000
    assert orch_hooks.record_usage_ok(a, 600) is True
    assert orch_hooks.record_usage_ok(a, 600) is False
    assert a.token_budget.consumed == [600, 600]


def test_default_llm_fn_is_swappable_and_used_by_council(restore, tmp_path):
    ports.set_config_port(_FakeConfigPort(
        council={"members": [{"id": "fake-model", "weight": 1.0}],
                 "thresholds": {}, "peer_eval": "never", "min_quorum": 1}))

    def fake_llm(member, system, user):
        return ('{"self_scores":{"correctness":5,"safety":5,"style":5,'
                '"tests":5,"complexity":5},"recommendation":"approve"}')

    ports.set_default_llm_fn(fake_llm)
    from agentic_system.council import CouncilService, CouncilRequest
    svc = CouncilService(str(tmp_path / "c.db"), members=None, peer_eval="never")
    dec = svc.review(CouncilRequest(subject_type="PR", subject_ref={"r": "x"},
                                    content="diff", risk_level="low"))
    assert dec.decision == "APPROVE"
    svc.close()


def test_council_without_llm_degrades_to_rework(restore, tmp_path):
    # No default LLM registered -> each member's call raises inside _stage1,
    # which the council catches (member loses its vote) -> insufficient quorum
    # -> REWORK. The error is surfaced as a warning, never propagated.
    ports.reset_ports_for_tests()  # no default llm
    ports.set_config_port(_FakeConfigPort(council={"members": [{"id": "m", "weight": 1.0}],
                                                   "thresholds": {}, "peer_eval": "never",
                                                   "min_quorum": 1}))
    from agentic_system.council import CouncilService, CouncilRequest
    svc = CouncilService(str(tmp_path / "c.db"), members=None, peer_eval="never")
    dec = svc.review(CouncilRequest(subject_type="PR", subject_ref={}, content="d", risk_level="low"))
    assert dec.decision == "REWORK"
    assert "insufficient_quorum" in (dec.reason or "")
    svc.close()


def test_register_sweeps_uses_cron_port(restore, tmp_path):
    ports.set_config_port(_FakeConfigPort(enabled=True, db=str(tmp_path / "e.db")))
    cron = _FakeCronPort(scripts_dir=str(tmp_path / "scripts"))
    ports.set_cron_port(cron)
    from agentic_system.sweeps import register_sweeps
    out = register_sweeps()
    assert out["ok"] is True
    assert len(out["created"]) == 4
    # idempotent
    cron.names = list(out["created"])
    out2 = register_sweeps()
    assert out2["created"] == [] and len(out2["skipped"]) == 4


def test_register_sweeps_without_cron_port_reports_error(restore):
    ports.reset_ports_for_tests()
    from agentic_system.sweeps import register_sweeps
    out = register_sweeps()
    assert out["ok"] is False and "CronPort" in out["error"]


def test_get_port_raises_when_none_registered(restore):
    ports.reset_ports_for_tests()
    with pytest.raises(RuntimeError, match="ConfigPort"):
        ports.get_config_port()
    with pytest.raises(RuntimeError, match="TokenBudgetPort"):
        ports.get_token_budget_port()
    with pytest.raises(RuntimeError, match="CronPort"):
        ports.get_cron_port()
    with pytest.raises(RuntimeError, match="default LLM"):
        ports.get_default_llm_fn()


def test_protocols_are_runtime_checkable():
    assert isinstance(_FakeConfigPort(), ports.ConfigPort)
    assert isinstance(_FakeTokenBudgetPort(), ports.TokenBudgetPort)
    assert isinstance(_FakeCronPort(), ports.CronPort)