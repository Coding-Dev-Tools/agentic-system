"""Durable event bus over the SQLite event store (outbox pattern).

``publish()`` appends to the store (the outbox) and synchronously notifies
in-process listeners (used by the gateway to bridge onto the existing
workspace SSE stream). Cross-process consumers use ``subscribe()`` — a
background poll loop with a per-consumer durable cursor (at-least-once).

Poll interval 250–500 ms is fine at our scale (handoff §3.2).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional, Sequence

from .envelope import EventEnvelope
from .store import EventStore

logger = logging.getLogger("agentic_system.events")

Handler = Callable[[EventEnvelope], None]

# After this many consecutive failures on the same event, skip it (poison pill).
_MAX_HANDLER_FAILURES = 5


class Subscription:
    def __init__(self, thread: threading.Thread, stop_flag: threading.Event):
        self._thread = thread
        self._stop = stop_flag

    def stop(self, join_timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout=join_timeout)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()


class EventBus:
    def __init__(self, store: EventStore, source: str = "hermes"):
        self.store = store
        self.source = source
        self._listeners: list[Handler] = []
        self._listeners_lock = threading.Lock()

    # ── publish ──────────────────────────────────────────────────────────
    def publish(
        self,
        type: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        aggregate_type: Optional[str] = None,
        aggregate_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        priority: str = "normal",
        target: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
        source: Optional[str] = None,
    ) -> EventEnvelope:
        env = EventEnvelope(
            type=type,
            payload=payload or {},
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            priority=priority,
            target=target,
            ttl_seconds=ttl_seconds,
            source=source or self.source,
        )
        return self.publish_envelope(env)

    def publish_envelope(self, env: EventEnvelope) -> EventEnvelope:
        self.store.append(env)
        self._notify(env)
        return env

    # ── in-process listeners (SSE bridge etc.) ───────────────────────────
    def add_listener(self, fn: Handler) -> None:
        with self._listeners_lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def remove_listener(self, fn: Handler) -> None:
        with self._listeners_lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def _notify(self, env: EventEnvelope) -> None:
        with self._listeners_lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(env)
            except Exception:  # listeners must never break publish
                logger.exception("event listener failed for %s", env.type)

    # ── durable cross-process consumption ────────────────────────────────
    def subscribe(
        self,
        consumer_name: str,
        handler: Handler,
        types: Optional[Sequence[str]] = None,
        poll_interval: float = 0.3,
        batch_size: int = 100,
    ) -> Subscription:
        """Start a background poll loop. At-least-once delivery:
        the offset is committed only after ``handler`` returns."""
        stop = threading.Event()

        def _loop() -> None:
            failures_at_seq: tuple[int, int] = (0, 0)  # (seq, count)
            while not stop.is_set():
                try:
                    offset = self.store.get_offset(consumer_name)
                    batch = self.store.read_since(offset, limit=batch_size, types=types)
                    if not batch:
                        stop.wait(poll_interval)
                        continue
                    for seq, env in batch:
                        if stop.is_set():
                            return
                        try:
                            handler(env)
                            self.store.commit_offset(consumer_name, seq)
                            failures_at_seq = (0, 0)
                        except Exception:
                            logger.exception(
                                "consumer %s failed on seq=%s type=%s",
                                consumer_name, seq, env.type,
                            )
                            prev_seq, count = failures_at_seq
                            count = count + 1 if prev_seq == seq else 1
                            failures_at_seq = (seq, count)
                            if count >= _MAX_HANDLER_FAILURES:
                                logger.error(
                                    "consumer %s skipping poison event seq=%s after %d failures",
                                    consumer_name, seq, count,
                                )
                                self.store.commit_offset(consumer_name, seq)
                                failures_at_seq = (0, 0)
                            else:
                                stop.wait(min(poll_interval * count, 5.0))
                            break  # re-read batch from committed offset
                except Exception:
                    logger.exception("consumer %s poll loop error", consumer_name)
                    stop.wait(max(poll_interval, 1.0))

        t = threading.Thread(
            target=_loop, name=f"event-consumer-{consumer_name}", daemon=True
        )
        t.start()
        return Subscription(t, stop)


__all__ = ["EventBus", "Subscription"]
