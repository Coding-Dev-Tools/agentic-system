"""Adapter seam between the framework-agnostic agentic-system core and the host.

The core (``agentic_system.events``, ``state_machine``, ``breakers``,
``no_progress``, ``council``, ``workflow``, ``orchestration_status``,
``sweeps``) depends only on the stdlib + pydantic. The host supplies four needs
through Protocol interfaces registered here:

1. **ConfigPort** — is orchestration enabled, where is the events DB, what
   council members/thresholds are configured, per-state tool policy overrides.
2. **TokenBudgetPort** — the per-task cumulative token-counter primitive.
3. **LLMPort** — one structured LLM call per council member. Adapters should
   accept a keyword-only ``timeout_seconds`` value so provider I/O observes the
   council deadline; legacy three-argument callables remain supported.
4. **CronPort** — register periodic sweep jobs (scripts dir, list/create).

A host calls ``set_config_port`` / ``set_token_budget_port`` /
``set_default_llm_fn`` / ``set_cron_port`` at startup. Tests register fakes.
This seam is what makes the package reusable from any Python agent runtime,
not just Hermes.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol, Sequence, Union, runtime_checkable


@runtime_checkable
class ConfigPort(Protocol):
    def orchestration_enabled(self) -> bool: ...
    def events_db_path(self) -> str: ...
    def council_config(self) -> Optional[dict[str, Any]]: ...
    def state_tool_policy(self) -> Optional[dict]: ...


@runtime_checkable
class TokenBudgetPort(Protocol):
    """Factory for the per-task token-budget primitive.

    The returned object must expose ``consume(tokens: int) -> None``,
    ``exceeded: bool``, ``used: int``, ``max_total: int``.
    """

    def make(self, max_tokens: int) -> Any: ...


@runtime_checkable
class CronPort(Protocol):
    """Host cron registration for the periodic sweeps."""

    def scripts_dir(self) -> str: ...
    def list_job_names(self) -> Sequence[str]: ...
    def create_job(self, *, name: str, schedule: str, script: str,
                   workdir: str) -> None: ...


LegacyLLMFn = Callable[[Any, str, str], str]


class DeadlineAwareLLMFn(Protocol):
    """Council adapter that cooperatively observes the remaining deadline."""

    def __call__(
        self,
        member: Any,
        system_prompt: str,
        user_prompt: str,
        *,
        timeout_seconds: float,
    ) -> str: ...


LLMFn = Union[LegacyLLMFn, DeadlineAwareLLMFn]


# ── Swappable process-wide registry ───────────────────────────────────────

_config_port: Optional[ConfigPort] = None
_token_budget_port: Optional[TokenBudgetPort] = None
_cron_port: Optional[CronPort] = None
_default_llm_fn: Optional[LLMFn] = None


def get_config_port() -> ConfigPort:
    if _config_port is None:
        raise RuntimeError(
            "no ConfigPort registered — call set_config_port(...) at host startup "
            "(e.g. an adapter that reads your config). See agentic_system.ports.")
    return _config_port


def get_token_budget_port() -> TokenBudgetPort:
    if _token_budget_port is None:
        raise RuntimeError(
            "no TokenBudgetPort registered — call set_token_budget_port(...) "
            "at host startup. See agentic_system.ports.")
    return _token_budget_port


def get_cron_port() -> CronPort:
    if _cron_port is None:
        raise RuntimeError(
            "no CronPort registered — call set_cron_port(...) at host startup, "
            "or avoid register_sweeps(). See agentic_system.ports.")
    return _cron_port


def get_default_llm_fn() -> LLMFn:
    if _default_llm_fn is None:
        raise RuntimeError(
            "council has no default LLM — either pass llm_fn= to CouncilService "
            "or register one via set_default_llm_fn(...). See agentic_system.ports.")
    return _default_llm_fn


def set_config_port(port: ConfigPort) -> None:
    global _config_port
    _config_port = port


def set_token_budget_port(port: TokenBudgetPort) -> None:
    global _token_budget_port
    _token_budget_port = port


def set_cron_port(port: CronPort) -> None:
    global _cron_port
    _cron_port = port


def set_default_llm_fn(fn: LLMFn) -> None:
    global _default_llm_fn
    _default_llm_fn = fn


def reset_ports_for_tests() -> None:
    """Drop every registered port/LLM (restore the "nothing registered" state)."""
    global _config_port, _token_budget_port, _cron_port, _default_llm_fn
    _config_port = None
    _token_budget_port = None
    _cron_port = None
    _default_llm_fn = None


__all__ = [
    "ConfigPort", "TokenBudgetPort", "CronPort",
    "LegacyLLMFn", "DeadlineAwareLLMFn", "LLMFn",
    "get_config_port", "get_token_budget_port", "get_cron_port",
    "get_default_llm_fn",
    "set_config_port", "set_token_budget_port", "set_cron_port",
    "set_default_llm_fn", "reset_ports_for_tests",
]