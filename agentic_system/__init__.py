"""agentic-system: a framework-agnostic orchestration layer for autonomous agents.

Durable event store, deterministic agent FSM, three-level circuit breakers,
no-progress detection, workflow DAG engine, model council, periodic sweeps,
and a read-only status/health CLI — all behind swappable host adapter ports
(ConfigPort / TokenBudgetPort / LLMPort / CronPort / EngraphisPort) so the
layer drops into any Python agent runtime, not just Hermes.

Quick start:
    pip install agentic-system
    from agentic_system import ports
    ports.set_config_port(MyConfig())        # implement ConfigPort
    ports.set_token_budget_port(MyBudget())  # implement TokenBudgetPort
    ports.set_default_llm_fn(my_llm)         # for the model council
    ports.set_cron_port(MyCron())            # for periodic sweeps
    # then use events/state_machine/breakers/council/workflow/sweeps/status

See README.md for the full wiring guide.
"""

from __future__ import annotations

__version__ = "0.3.0"

from . import ports  # noqa: F401  (the adapter seam; host registers here)

__all__ = ["ports", "__version__"]