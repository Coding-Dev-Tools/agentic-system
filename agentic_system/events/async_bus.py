"""Async-native event bus and council for high-throughput agents."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Optional

from agentic_system.events import connect, ensure_state_tables, now_iso
from agentic_system.ports import get_config_port

logger = logging.getLogger("agentic_system.events.async_bus")


@dataclass
class AsyncEvent:
    type: str
    payload: dict
    aggregate_type: str = ""
    aggregate_id: str = ""
    correlation_id: Optional[str] = None
    priority: str = "normal"
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")


class AsyncEventBus:
    """Async-native event bus with SQLite persistence and pub/sub."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = connect(db_path)
        ensure_state_tables(self._conn)
        self._subs: dict[str, list[Callable[[AsyncEvent], Any]]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, event: AsyncEvent) -> str:
        """Persist and fan-out an event."""
        self._conn.execute(
            """INSERT INTO events (event_id, type, payload, agg_type, agg_id,
                                   corr_id, priority, ts)
               VALUES (?,?,?,?,?,?,?,?)""",
            (event.event_id, event.type, json.dumps(event.payload),
             event.aggregate_type, event.aggregate_id,
             event.correlation_id or "", event.priority, event.ts),
        )
        self._conn.commit()

        # Fan-out (non-blocking)
        asyncio.create_task(self._fan_out(event))
        return event.event_id

    async def _fan_out(self, event: AsyncEvent) -> None:
        async with self._lock:
            handlers = list(self._subs.get(event.type, []))
        for h in handlers:
            try:
                if asyncio.iscoroutinefunction(h):
                    await h(event)
                else:
                    h(event)
            except Exception:
                logger.exception("async handler %s failed", h)

    def subscribe(self, event_type: str, handler: Callable[[AsyncEvent], Any]) -> Callable[[], None]:
        """Register handler. Returns unsubscribe function."""
        self._subs.setdefault(event_type, []).append(handler)

        def unsubscribe() -> None:
            self._subs[event_type].remove(handler)
        return unsubscribe

    async def query(
        self,
        *,
        aggregate_type: Optional[str] = None,
        aggregate_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 100,
    ) -> list[AsyncEvent]:
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
        return [
            AsyncEvent(
                event_id=r["event_id"], type=r["type"],
                payload=json.loads(r["payload"]),
                aggregate_type=r["agg_type"], aggregate_id=r["agg_id"],
                correlation_id=r["corr_id"] or None,
                priority=r["priority"], ts=r["ts"],
            ) for r in rows
        ]

    def close(self) -> None:
        self._conn.close()


@asynccontextmanager
async def async_bus(db_path: Optional[str] = None) -> AsyncGenerator[AsyncEventBus, None]:
    """Context manager for async event bus."""
    bus = AsyncEventBus(db_path or get_config_port().events_db_path())
    try:
        yield bus
    finally:
        bus.close()


# ── Async Council Integration ─────────────────────────────────────────────


async def review_async(
    request: CouncilRequest,
    *,
    db_path: Optional[str] = None,
    members: Optional[list[dict]] = None,
    thresholds: Optional[dict] = None,
    peer_eval: str = "high_risk_only",
    min_quorum: int = 2,
    llm_fn: Optional[Callable] = None,
    persist_hook: Optional[Callable] = None,
) -> CouncilDecision:
    """Convenience: run a council review in a thread pool (CouncilService is sync)."""
    from agentic_system.council import CouncilService, CouncilRequest
    from agentic_system.council.schemas import CouncilDecision
    loop = asyncio.get_event_loop()

    def _sync():
        svc = CouncilService(
            db_path or get_config_port().events_db_path(),
            members=members, thresholds=thresholds,
            peer_eval=peer_eval, min_quorum=min_quorum,
            llm_fn=llm_fn, persist_hook=persist_hook,
        )
        try:
            return svc.review(request)
        finally:
            svc.close()

    return await loop.run_in_executor(None, _sync)