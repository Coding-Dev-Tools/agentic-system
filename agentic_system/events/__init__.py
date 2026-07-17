"""Durable event store (SQLite WAL) + event bus.

The event bus is a thin in-process pub/sub with optional SQLite persistence.
Subscribers register via ``subscribe()``; events are appended to the events
table (append-only, never deleted — pruning archives to JSONL first).
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from agentic_system.ports import get_config_port, get_engraphis_port

logger = logging.getLogger("agentic_system.events")

# ── Process-wide bus cache (avoids a sqlite conn per tool call) ─────────
_bus_cache: Optional["EventBus"] = None


def get_bus(db_path: Optional[str] = None) -> "EventBus":
    """Process-wide cached EventBus over the orchestration events DB.

    A fresh bus opens its own SQLite connection; caching one avoids opening
    a connection on every tool dispatch in the hot path. The cache is keyed
    on the default events DB; an explicit ``db_path`` bypasses the cache
    (used by tests).
    """
    global _bus_cache
    if _bus_cache is not None and db_path is None:
        return _bus_cache
    bus = EventBus(db_path or events_db_path())
    if db_path is None:
        _bus_cache = bus
    return bus


def events_db_path() -> str:
    """Resolve the events DB path from the host's ConfigPort."""
    return get_config_port().events_db_path()


def reset_bus_for_tests() -> None:
    """Drop the cached bus (mirror of ports.reset_ports_for_tests)."""
    global _bus_cache
    if _bus_cache is not None:
        try:
            _bus_cache.close()
        except Exception:
            pass
    _bus_cache = None


# ── Event envelope ───────────────────────────────────────────────────────

@dataclass
class Event:
    type: str
    payload: dict
    aggregate_type: str = ""
    aggregate_id: str = ""
    correlation_id: Optional[str] = None
    priority: str = "normal"          # normal | high | critical
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

    def to_row(self) -> tuple:
        return (self.event_id, self.type, json.dumps(self.payload),
                self.aggregate_type, self.aggregate_id,
                self.correlation_id or "", self.priority, self.ts)


# ── Event bus ─────────────────────────────────────────────────────────────

class EventBus:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = self._connect(db_path)
        self._ensure_schema()
        self._subs: dict[str, list[Callable[[Event], None]]] = {}
        self._lock = threading.Lock()

    def _connect(self, db_path: str):
        import sqlite3
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                event_id     TEXT PRIMARY KEY,
                type         TEXT NOT NULL,
                payload      TEXT NOT NULL,
                agg_type     TEXT NOT NULL,
                agg_id       TEXT NOT NULL,
                corr_id      TEXT,
                priority     TEXT NOT NULL DEFAULT 'normal',
                ts           TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_agg
                ON events(agg_type, agg_id);
            CREATE INDEX IF NOT EXISTS idx_events_corr
                ON events(corr_id);
            CREATE INDEX IF NOT EXISTS idx_events_ts
                ON events(ts);
        """)
        self._conn.commit()

    def subscribe(self, event_type: str,
                  handler: Callable[[Event], None]) -> Callable[[], None]:
        """Register ``handler`` for ``event_type``. Returns an unsubscribe fn."""
        with self._lock:
            self._subs.setdefault(event_type, []).append(handler)
        def unsubscribe() -> None:
            with self._lock:
                if event_type in self._subs:
                    try:
                        self._subs[event_type].remove(handler)
                    except ValueError:
                        pass
        return unsubscribe

    def publish(self, event_type: str, payload: dict,
                aggregate_type: str = "", aggregate_id: str = "",
                correlation_id: Optional[str] = None,
                priority: str = "normal") -> str:
        """Persist + fan-out an event. Returns the event_id."""
        evt = Event(
            type=event_type,
            payload=payload,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            correlation_id=correlation_id,
            priority=priority,
        )
        self._conn.execute(
            """INSERT INTO events (event_id, type, payload, agg_type, agg_id,
                                   corr_id, priority, ts)
               VALUES (?,?,?,?,?,?,?,?)""",
            evt.to_row(),
        )
        self._conn.commit()

        # Fan-out to subscribers (non-blocking, best-effort)
        for h in self._subs.get(event_type, []):
            try:
                h(evt)
            except Exception:
                logger.exception("event handler %s failed", h)
        return evt.event_id

    def query(self, *, aggregate_type: Optional[str] = None,
              aggregate_id: Optional[str] = None,
              correlation_id: Optional[str] = None,
              since: Optional[str] = None,
              limit: int = 100) -> list[Event]:
        """Read events back (for replay/debug)."""
        sql = "SELECT * FROM events WHERE 1=1"
        params: list = []
        if aggregate_type:
            sql += " AND agg_type=?"
            params.append(aggregate_type)
        if aggregate_id:
            sql += " AND agg_id=?"
            params.append(aggregate_id)
        if correlation_id:
            sql += " AND corr_id=?"
            params.append(correlation_id)
        if since:
            sql += " AND ts>=?"
            params.append(since)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [Event(
            event_id=r["event_id"], type=r["type"],
            payload=json.loads(r["payload"]),
            aggregate_type=r["agg_type"], aggregate_id=r["agg_id"],
            correlation_id=r["corr_id"] or None,
            priority=r["priority"], ts=r["ts"]
        ) for r in rows]

    def close(self) -> None:
        self._conn.close()


# ── State tables (breaker/workflow/council) ──────────────────────────────

STATE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS breakers (
        level TEXT NOT NULL, key TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'CLOSED',
        reason TEXT DEFAULT '', opened_at TEXT, half_open_at TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY (level, key)
    );
    CREATE TABLE IF NOT EXISTS council_sessions (
        id TEXT PRIMARY KEY, subject_type TEXT, subject_ref TEXT,
        subject_hash TEXT, rubric_hash TEXT, status TEXT,
        confidence REAL DEFAULT 0, decision TEXT, session_json TEXT,
        engraphis_ref TEXT, created_at TEXT, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS workflow_instances (
        instance_id TEXT PRIMARY KEY, workflow_name TEXT, state TEXT,
        payload_json TEXT, claimed_by TEXT, claim_ts TEXT,
        created_at TEXT, updated_at TEXT, version INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS workflow_claims (
        instance_id TEXT, task_name TEXT, claimed_by TEXT, claim_ts TEXT,
        outcome TEXT, result_json TEXT,
        PRIMARY KEY (instance_id, task_name)
    );
    CREATE TABLE IF NOT EXISTS cron_jobs (
        name TEXT PRIMARY KEY, schedule TEXT, script TEXT, workdir TEXT,
        enabled INTEGER DEFAULT 1, last_run_at TEXT, next_run_at TEXT,
        last_status TEXT, last_error TEXT
    );
"""

def connect(db_path: str):
    import sqlite3
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_state_tables(conn) -> None:
    conn.executescript(STATE_SCHEMA)
    conn.commit()


__all__ = [
    "Event", "EventBus", "get_bus", "events_db_path", "reset_bus_for_tests",
    "connect", "ensure_state_tables", "now_iso",
    "AsyncEventBus", "AsyncEvent", "async_bus",
]
def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


# Imported last: async_bus does `from agentic_system.events import now_iso`,
# which is only defined above once this module is fully initialized. Importing
# it earlier triggers a circular-import ImportError.
from .async_bus import AsyncEventBus, AsyncEvent, async_bus