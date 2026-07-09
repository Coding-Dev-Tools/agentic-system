"""Three-level circuit breakers: agent / workflow / global (handoff §3.6).

Layering: the host watchdog (pm2/process liveness, every 10 min) is the layer
BELOW this and stays untouched; breakers are behavioral safety ABOVE it —
they stop agents from *doing* things, not processes from running.

States: CLOSED (normal) → OPEN (tripped) → HALF_OPEN (probation) → CLOSED.
Persisted in hermes_events.db so restarts are idempotent (handoff §6.5).

Global-open side effects:
- ``breaker.opened`` (priority=critical) on the event bus,
- an addendum line appended to ``_cowork_ops/ALERT.md`` (existing incident
  convention — watchdog owns the file, addenda are permitted),
- ``should_accept_task()`` / ``allow_high_impact()`` return False, which
  workflow workers and deploy-ish tools must check before acting.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from agentic_system.events.state_tables import connect, ensure_state_tables, now_iso

logger = logging.getLogger("hermes.breakers")

CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"
LEVELS = ("agent", "workflow", "global")
GLOBAL_KEY = "system"

# Generic default: no alert file (a host passes alert_path=... to
# BreakerRegistry, e.g. Hermes points at its _cowork_ops/ALERT.md).
_DEFAULT_ALERT: Optional[str] = None

# Tool-name patterns blocked when the global breaker is OPEN (handoff §3.1
# high-impact set). Kept in sync with agent.state_machine._HIGH_IMPACT.
HIGH_IMPACT_PATTERNS = (
    "deploy*", "*git_push*", "*push_to_remote*",
    "publish*", "release*", "*prod*deploy*",
)

# High-impact shell commands inside the `terminal` tool — remote-publishing
# or infra-deploy operations that an OPEN global breaker must refuse. Tunable
# heuristic; only consulted when the breaker is OPEN, so false positives only
# matter during a deliberate "stop everything dangerous" incident state.
_COMMAND_HIGH_IMPACT = re.compile(
    r"""\b(?:
        git\s+push|
        (?:npm|yarn|pnpm)\s+publish|
        docker\s+push|
        kubectl\s+(?:apply|rollout|delete)|
        helm\s+(?:upgrade|install|uninstall)|
        terraform\s+apply|
        pulumi\s+up
    )\b""",
    re.VERBOSE | re.IGNORECASE,
)


# ── Process-wide cached registry (avoids a sqlite conn per tool call) ──────
_registry_cache: Optional["BreakerRegistry"] = None


def get_registry(db_path: Optional[str] = None) -> "BreakerRegistry":
    """Process-wide cached BreakerRegistry over the orchestration events DB.

    A fresh registry opens its own SQLite connection; caching one avoids
    opening a connection on every tool dispatch in the hot path. The cache
    is keyed on the default events DB; an explicit ``db_path`` bypasses the
    cache (used by tests).
    """
    global _registry_cache
    if _registry_cache is not None and db_path is None:
        return _registry_cache
    from agentic_system.events.hooks import events_db_path, get_bus
    reg = BreakerRegistry(db_path or events_db_path(), bus=get_bus())
    if db_path is None:
        _registry_cache = reg
    return reg


def reset_registry_for_tests() -> None:
    """Drop the cached registry (mirror of hooks.reset_bus_for_tests)."""
    global _registry_cache
    if _registry_cache is not None:
        try:
            _registry_cache.close_conn()
        except Exception:
            pass
    _registry_cache = None


def high_impact_block_message(tool_name: str,
                             tool_args: Optional[dict] = None) -> Optional[str]:
    """Return a block message if ``tool_name`` is high-impact and the global
    circuit breaker is OPEN. ``None`` means "allow".

    No-op unless orchestration is enabled, so the interactive turn loop is
    unaffected when the layer is off (default). Never raises.

    Two gates:
    1. *Named* high-impact tools (deploy/push/publish) — matched against
       HIGH_IMPACT_PATTERNS by tool name.
    2. The ``terminal`` tool running a high-impact shell command (git push,
       npm publish, docker push, kubectl/helm/terraform apply, ...) — matched
       against _COMMAND_HIGH_IMPACT on the ``command`` arg. Closes the gap
       where a push/publish happens inside a shell command rather than via
       a dedicated tool.
    """
    try:
        from agentic_system.events.hooks import orchestration_enabled
        if not orchestration_enabled():
            return None
        named_hit = any(fnmatch.fnmatch(tool_name, p) for p in HIGH_IMPACT_PATTERNS)
        command_hit = False
        if tool_name == "terminal" and isinstance(tool_args, dict):
            cmd = tool_args.get("command")
            if isinstance(cmd, str) and _COMMAND_HIGH_IMPACT.search(cmd):
                command_hit = True
        if not (named_hit or command_hit):
            return None
        if get_registry().allow_high_impact():
            return None
        return (
            "blocked by the OPEN global circuit breaker: high-impact tools "
            "(deploy/push/publish) are suspended until the breaker is closed "
            "via BreakerRegistry.close('global','system')."
        )
    except Exception:
        logger.debug("high_impact_block_message failed", exc_info=True)
        return None


class BreakerRegistry:
    def __init__(self, db_path: str, bus: Any = None,
                 alert_path: Optional[str] = None):
        self._conn = connect(db_path)
        ensure_state_tables(self._conn)
        self.bus = bus
        self.alert_path = str(alert_path) if alert_path else _DEFAULT_ALERT

    # ── queries ──────────────────────────────────────────────────────────
    def state(self, level: str, key: str) -> str:
        row = self._conn.execute(
            "SELECT state FROM breakers WHERE level=? AND key=?", (level, key)
        ).fetchone()
        return row["state"] if row else CLOSED

    def is_open(self, level: str, key: str) -> bool:
        return self.state(level, key) == OPEN

    def should_accept_task(self, agent_id: Optional[str] = None,
                           workflow: Optional[str] = None) -> bool:
        """Check all three levels before any task claim (handoff §3.6)."""
        if self.is_open("global", GLOBAL_KEY):
            return False
        if workflow and self.is_open("workflow", workflow):
            return False
        if agent_id and self.is_open("agent", agent_id):
            return False
        return True

    def allow_high_impact(self) -> bool:
        """Deploy/push/publish actions are blocked whenever global is OPEN."""
        return not self.is_open("global", GLOBAL_KEY)

    def snapshot(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM breakers ORDER BY level, key").fetchall()
        return [dict(r) for r in rows]

    # ── transitions ──────────────────────────────────────────────────────
    def _set(self, level: str, key: str, state: str, reason: str = "") -> None:
        if level not in LEVELS:
            raise ValueError(f"unknown breaker level {level!r}")
        ts = now_iso()
        self._conn.execute(
            """INSERT INTO breakers (level, key, state, reason, opened_at,
                                     half_open_at, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(level, key) DO UPDATE SET
                 state=excluded.state,
                 reason=excluded.reason,
                 opened_at=CASE WHEN excluded.state='OPEN'
                                THEN excluded.updated_at ELSE breakers.opened_at END,
                 half_open_at=CASE WHEN excluded.state='HALF_OPEN'
                                THEN excluded.updated_at ELSE breakers.half_open_at END,
                 updated_at=excluded.updated_at""",
            (level, key, state, reason,
             ts if state == OPEN else None,
             ts if state == HALF_OPEN else None, ts),
        )
        self._conn.commit()

    def open(self, level: str, key: str, reason: str) -> None:
        already = self.is_open(level, key)
        self._set(level, key, OPEN, reason)
        logger.warning("breaker OPEN level=%s key=%s reason=%s", level, key, reason)
        self._emit("breaker.opened", level, key, reason,
                   priority="critical" if level == "global" else "high")
        if level == "global" and not already:
            self._write_alert(reason)

    def half_open(self, level: str, key: str, reason: str = "probation") -> None:
        self._set(level, key, HALF_OPEN, reason)
        self._emit("breaker.half_open", level, key, reason)

    def close(self, level: str, key: str, reason: str = "recovered") -> None:
        self._set(level, key, CLOSED, reason)
        self._emit("breaker.closed", level, key, reason)

    # ── side effects ─────────────────────────────────────────────────────
    def _emit(self, type: str, level: str, key: str, reason: str,
              priority: str = "high") -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(
                type,
                {"level": level, "key": key, "reason": reason},
                aggregate_type="Breaker", aggregate_id=f"{level}:{key}",
                priority=priority,
            )
        except Exception:
            logger.exception("breaker event emit failed")

    def _write_alert(self, reason: str) -> None:
        try:
            if self.alert_path is None:
                return  # no alert file configured
            p = Path(self.alert_path)
            if not p.parent.exists():
                logger.warning("alert dir missing, skipping alert file write: %s", p)
                return
            line = (f"\n[agentic-breaker {now_iso()}] GLOBAL CIRCUIT BREAKER OPEN: "
                    f"{reason} — all high-impact actions (deploy/push/publish) "
                    f"blocked until closed via BreakerRegistry.close('global','system'). "
                    f"State: the orchestration events DB breakers table.\n")
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            logger.exception("failed to write ALERT.md addendum")

    def close_conn(self) -> None:
        self._conn.close()


__all__ = [
    "BreakerRegistry", "CLOSED", "OPEN", "HALF_OPEN", "GLOBAL_KEY",
    "HIGH_IMPACT_PATTERNS", "get_registry", "reset_registry_for_tests",
    "high_impact_block_message",
]
