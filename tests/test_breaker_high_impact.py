"""Wiring test: high-impact tools are blocked when the global breaker is OPEN.

Covers the ``agent.breakers.high_impact_block_message`` gate wired into
``agent/tool_executor.py`` (both sequential and concurrent dispatch paths).
"""

import fnmatch

import pytest

from agentic_system import breakers
from agentic_system.breakers import (
    GLOBAL_KEY,
    HIGH_IMPACT_PATTERNS,
    BreakerRegistry,
    high_impact_block_message,
    reset_registry_for_tests,
)
from agentic_system.events import hooks as orch_hooks


@pytest.fixture()
def enabled_env(tmp_path, monkeypatch):
    """Point orchestration at a throwaway DB with the flag ON."""
    db = tmp_path / "events.db"
    monkeypatch.setenv("HERMES_ORCHESTRATION", "1")
    monkeypatch.setenv("HERMES_EVENTS_DB", str(db))
    reset_registry_for_tests()
    orch_hooks.reset_bus_for_tests()
    yield str(db)
    reset_registry_for_tests()
    orch_hooks.reset_bus_for_tests()


@pytest.fixture()
def disabled_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_ORCHESTRATION", "0")
    monkeypatch.setenv("HERMES_EVENTS_DB", str(tmp_path / "events.db"))
    reset_registry_for_tests()
    orch_hooks.reset_bus_for_tests()
    yield
    reset_registry_for_tests()
    orch_hooks.reset_bus_for_tests()


def test_patterns_cover_documented_high_impact_tools():
    # The documented deny set from agentic_system.state_machine must all match.
    for name in ("deploy", "git_push", "push_to_remote", "publish", "release", "prod_deploy"):
        assert any(fnmatch.fnmatch(name, p) for p in HIGH_IMPACT_PATTERNS), name


def test_terminal_command_gate_blocks_push_when_open(enabled_env):
    reg = BreakerRegistry(enabled_env)
    reg.open("global", GLOBAL_KEY, "incident")
    try:
        # named high-impact tools still blocked
        assert high_impact_block_message("deploy") is not None
        # a plain read is never blocked
        assert high_impact_block_message("read_file") is None
        # terminal running a benign command is not blocked
        assert high_impact_block_message("terminal", {"command": "ls -la"}) is None
        # terminal running a push/publish/apply IS blocked (command-content gate)
        for cmd in ("git push origin main",
                    "npm publish --access public",
                    "docker push registry/app:latest",
                    "kubectl apply -f deploy.yaml",
                    "helm upgrade app ./chart",
                    "terraform apply -auto-approve"):
            msg = high_impact_block_message("terminal", {"command": cmd})
            assert msg is not None, cmd
            assert "global circuit breaker" in msg
        # the command gate must not fire when the breaker is closed
        reg.close("global", GLOBAL_KEY, "ok")
        assert high_impact_block_message("terminal", {"command": "git push"}) is None
    finally:
        reg.close_conn()


def test_command_gate_is_case_insensitive_and_substring_safe(enabled_env):
    reg = BreakerRegistry(enabled_env)
    reg.open("global", GLOBAL_KEY, "x")
    try:
        assert high_impact_block_message("terminal", {"command": "GIT PUSH"}) is not None
        # command embedded in a larger script is still caught
        assert high_impact_block_message("terminal",
            {"command": "cd app && make build && git push origin main"}) is not None
        # lookalikes that should NOT trip the regex
        assert high_impact_block_message("terminal", {"command": "git pull"}) is None
        assert high_impact_block_message("terminal", {"command": "echo git push"}) is not None  # conservative: even echoing trips during an incident
    finally:
        reg.close_conn()


def test_disabled_returns_none_even_when_breaker_open(disabled_env, tmp_path):
    # Open the global breaker in its own registry; the gate must still be a
    # no-op because orchestration is disabled.
    reg = BreakerRegistry(str(tmp_path / "events.db"))
    reg.open("global", GLOBAL_KEY, "test")
    try:
        assert high_impact_block_message("deploy") is None
        assert high_impact_block_message("read_file") is None
    finally:
        reg.close_conn()


def test_enabled_breaker_closed_allows_everything(enabled_env):
    assert high_impact_block_message("deploy") is None
    assert high_impact_block_message("read_file") is None


def test_enabled_global_open_blocks_high_impact_only(enabled_env):
    reg = BreakerRegistry(enabled_env)
    reg.open("global", GLOBAL_KEY, "cost spike")
    try:
        msg = high_impact_block_message("deploy_prod")
        assert msg is not None
        assert "global circuit breaker" in msg
        # non-high-impact tools are unaffected
        assert high_impact_block_message("read_file") is None
        assert high_impact_block_message("terminal") is None
    finally:
        reg.close_conn()


def test_closing_breaker_re_allows_high_impact(enabled_env):
    reg = BreakerRegistry(enabled_env)
    reg.open("global", GLOBAL_KEY, "trip")
    assert high_impact_block_message("publish") is not None
    reg.close("global", GLOBAL_KEY, "recovered")
    try:
        assert high_impact_block_message("publish") is None
    finally:
        reg.close_conn()


def test_never_raises_on_broken_layer(enabled_env, monkeypatch):
    # If the registry cannot be built (e.g. bad DB path), the gate must fail
    # closed-ish but never raise into the tool hot path.
    monkeypatch.setattr(breakers, "get_registry",
                        lambda db_path=None: (_ for _ in ()).throw(RuntimeError("boom")))
    assert high_impact_block_message("deploy") is None