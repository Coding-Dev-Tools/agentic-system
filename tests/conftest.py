"""Test config: register ports so the ported orchestration tests work without
a Hermes host.

The package ships no HermesConfigPort (the host registers its own). For tests we
register an env-reading ConfigPort that honours the same HERMES_ORCHESTRATION /
HERMES_EVENTS_DB escape hatches the original Hermes port did, so the ported
tests (which set those env vars via monkeypatch) run unchanged. A minimal
TokenBudgetPort is registered too.
"""

import os
from pathlib import Path

import pytest

from agentic_system import ports
from agentic_system.events import hooks


class _EnvConfigPort:
    def __init__(self) -> None:
        self._cwd = Path.cwd()

    def orchestration_enabled(self) -> bool:
        env = os.getenv("HERMES_ORCHESTRATION", "").strip().lower()
        if env in {"1", "true", "yes", "on"}:
            return True
        if env in {"0", "false", "no", "off"}:
            return False
        return False

    def events_db_path(self) -> str:
        env = os.getenv("HERMES_EVENTS_DB", "").strip()
        return env or str(self._cwd / "events.db")

    def council_config(self):
        return None

    def state_tool_policy(self):
        return None


class _FakeBudget:
    def __init__(self, max_tokens):
        self.max_total = max_tokens
        self.used = 0
        self.exceeded = False

    def consume(self, tokens):
        self.used += int(tokens or 0)
        self.exceeded = self.used >= self.max_total


class _FakeBudgetPort:
    def make(self, max_tokens):
        return _FakeBudget(max_tokens)


@pytest.fixture(autouse=True)
def _test_ports():
    ports.set_config_port(_EnvConfigPort())
    ports.set_token_budget_port(_FakeBudgetPort())
    hooks.reset_bus_for_tests()
    yield
    ports.reset_ports_for_tests()
    hooks.reset_bus_for_tests()