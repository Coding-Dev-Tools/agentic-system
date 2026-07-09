"""Test config: register ports so the orchestration tests work without a host.

The package ships no host ConfigPort (the host registers its own). For tests we
register an env-reading ConfigPort that honours the AGENTIC_ORCHESTRATION /
AGENTIC_EVENTS_DB escape hatches (with HERMES_* accepted as back-compat
aliases), so the ported tests (which set those env vars via monkeypatch) run
unchanged. A minimal TokenBudgetPort is registered too.
"""

import os
from pathlib import Path

import pytest

from agentic_system import ports
from agentic_system.events import hooks


def _env_or(primary: str, alias: str) -> str:
    """Read the ``primary`` env var, falling back to ``alias`` (back-compat)."""
    v = os.getenv(primary, "").strip()
    return v or os.getenv(alias, "").strip()


class _EnvConfigPort:
    def __init__(self) -> None:
        self._cwd = Path.cwd()

    def orchestration_enabled(self) -> bool:
        env = _env_or("AGENTIC_ORCHESTRATION", "HERMES_ORCHESTRATION").lower()
        if env in {"1", "true", "yes", "on"}:
            return True
        if env in {"0", "false", "no", "off"}:
            return False
        return False

    def events_db_path(self) -> str:
        env = _env_or("AGENTIC_EVENTS_DB", "HERMES_EVENTS_DB")
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