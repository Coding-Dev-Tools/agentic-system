"""Flag-gated glue between the host agent runtime and the orchestration layer.

Everything here is safe to call from the conversation loop hot path:
- ``emit()`` NEVER raises and is a no-op unless orchestration is enabled
  (the ConfigPort's ``orchestration_enabled()`` -- hosts typically read
  ``orchestration.enabled`` from their config; the test ConfigPort honours
  ``AGENTIC_ORCHESTRATION`` with ``HERMES_ORCHESTRATION`` as a back-compat alias).
- Config and the token-budget primitive are accessed through the adapter
  seam in ``ports`` (ConfigPort / TokenBudgetPort), so this module has NO
  direct dependency on any host's config or token-budget module -- another
  agent runtime can register its own ports and reuse the whole layer. The
  default DB is ``<cwd>/events.db`` when no port is registered (override via
  the ConfigPort's ``events_db_path()``, or ``AGENTIC_EVENTS_DB`` in the test
  ConfigPort; ``HERMES_EVENTS_DB`` accepted as a back-compat alias).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from agentic_system.ports import (  # the host-adapter seam
    get_config_port, get_token_budget_port,
    set_config_port, set_token_budget_port, reset_ports_for_tests,
)

logger = logging.getLogger("agentic_system.events")

_bus = None
_bus_lock = threading.Lock()


def orchestration_enabled() -> bool:
    try:
        return get_config_port().orchestration_enabled()
    except Exception:
        return False


def events_db_path() -> str:
    try:
        return get_config_port().events_db_path()
    except Exception:
        from pathlib import Path
        return str(Path.cwd() / "events.db")


def get_bus():
    """Singleton EventBus, or None when orchestration is disabled/broken."""
    global _bus
    if not orchestration_enabled():
        return None
    if _bus is not None:
        return _bus
    with _bus_lock:
        if _bus is None:
            try:
                from .bus import EventBus
                from .store import EventStore
                _bus = EventBus(EventStore(events_db_path()))
            except Exception:
                logger.exception("failed to init orchestration event bus")
                return None
    return _bus


def reset_bus_for_tests() -> None:
    global _bus
    with _bus_lock:
        if _bus is not None:
            try:
                _bus.store.close()
            except Exception:
                pass
        _bus = None


def emit(type: str, payload: Optional[dict[str, Any]] = None, **kw: Any) -> None:
    """Fire-and-forget event emission. Never raises, no-op when disabled."""
    try:
        bus = get_bus()
        if bus is None:
            return
        bus.publish(type, payload or {}, **kw)
    except Exception:
        logger.debug("orchestration emit failed for %s", type, exc_info=True)


# ── Turn lifecycle (called from conversation_loop, flag-gated) ────────────

def emit_turn_started(agent: Any, task_id: Optional[str] = None) -> None:
    emit(
        "turn.started",
        {
            "session_id": getattr(agent, "session_id", None),
            "model": getattr(agent, "model", None),
            "provider": getattr(agent, "provider", None),
            "task_id": task_id,
        },
        aggregate_type="Session",
        aggregate_id=str(getattr(agent, "session_id", "") or ""),
        correlation_id=task_id or None,
    )


def emit_turn_completed(
    agent: Any,
    task_id: Optional[str] = None,
    api_calls: Optional[int] = None,
    total_tokens: Optional[int] = None,
) -> None:
    emit(
        "turn.completed",
        {
            "session_id": getattr(agent, "session_id", None),
            "task_id": task_id,
            "api_calls": api_calls,
            "total_tokens": total_tokens,
        },
        aggregate_type="Session",
        aggregate_id=str(getattr(agent, "session_id", "") or ""),
        correlation_id=task_id or None,
    )


def emit_turn_failed(
    agent: Any, task_id: Optional[str] = None, reason: str = "",
) -> None:
    emit(
        "turn.failed",
        {
            "session_id": getattr(agent, "session_id", None),
            "task_id": task_id,
            "reason": reason,
        },
        aggregate_type="Session",
        aggregate_id=str(getattr(agent, "session_id", "") or ""),
        correlation_id=task_id or None,
        priority="high",
    )


# ── Token budget (Phase 1, enforced only when attached + enabled) ────────

def attach_token_budget(agent: Any, max_tokens: int) -> None:
    """Give ``agent`` a per-task token budget via the TokenBudgetPort seam."""
    agent.token_budget = get_token_budget_port().make(max_tokens)


def record_usage_ok(agent: Any, total_tokens: int) -> bool:
    """Accumulate usage into the agent's TokenBudget if present.

    Returns False when the budget is exhausted (caller should fail the task
    with reason="token_budget"). Never raises; returns True when no budget
    is attached or orchestration is disabled.
    """
    try:
        if not orchestration_enabled():
            return True
        budget = getattr(agent, "token_budget", None)
        if budget is None:
            return True
        budget.consume(int(total_tokens or 0))
        if budget.exceeded:
            emit(
                "budget.token_exhausted",
                {
                    "session_id": getattr(agent, "session_id", None),
                    "used": budget.used,
                    "max_total": budget.max_total,
                },
                priority="high",
            )
            return False
        return True
    except Exception:
        logger.debug("record_usage_ok failed", exc_info=True)
        return True


__all__ = [
    "orchestration_enabled", "events_db_path", "get_bus", "emit",
    "emit_turn_started", "emit_turn_completed", "emit_turn_failed",
    "attach_token_budget", "record_usage_ok", "reset_bus_for_tests",
    # adapter-seam passthroughs (host swappability)
    "set_config_port", "set_token_budget_port", "reset_ports_for_tests",
]
