"""Append-only SQLite event store (WAL) — system of record for control flow.

Separate DB from ``engraphis.db`` on purpose: memory has decay/consolidation
semantics; control flow needs exact ordering, replay, and no decay
(handoff §3.2). Single-box, SQLite-first — no Redis/NATS until multi-host.

Concurrency: one connection guarded by a lock, WAL mode, busy_timeout.
Multiple pm2 processes should each open their own EventStore; SQLite WAL
handles cross-process readers. If writer contention ever shows up, route
writes through the gateway (handoff §6.2).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

from .envelope import EventEnvelope

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    id             TEXT NOT NULL UNIQUE,
    type           TEXT NOT NULL,
    source         TEXT,
    target         TEXT,
    aggregate_type TEXT,
    aggregate_id   TEXT,
    correlation_id TEXT,
    causation_id   TEXT,
    created_at     TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    priority       TEXT NOT NULL DEFAULT 'normal',
    ttl_seconds    INTEGER,
    payload_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_corr ON events(correlation_id);
CREATE INDEX IF NOT EXISTS idx_events_agg  ON events(aggregate_type, aggregate_id);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE TABLE IF NOT EXISTS consumer_offsets (
    consumer_name TEXT PRIMARY KEY,
    last_seq      INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class EventStore:
    def __init__(self, db_path: str, timeout: float = 30.0):
        self.db_path = str(db_path)
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path, timeout=timeout, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ── writes ────────────────────────────────────────────────────────────
    def append(self, event: EventEnvelope) -> int:
        """Append one validated event; returns its monotonic ``seq``."""
        if not isinstance(event, EventEnvelope):
            raise TypeError("append() requires an EventEnvelope")
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO events (id, type, source, target, aggregate_type,
                       aggregate_id, correlation_id, causation_id, created_at,
                       schema_version, priority, ttl_seconds, payload_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.id, event.type, event.source, event.target,
                    event.aggregate_type, event.aggregate_id,
                    event.correlation_id, event.causation_id, event.created_at,
                    event.schema_version, event.priority, event.ttl_seconds,
                    json.dumps(event.payload, ensure_ascii=False, default=str),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def append_many(self, events: Iterable[EventEnvelope]) -> list[int]:
        return [self.append(e) for e in events]

    # ── reads ────────────────────────────────────────────────────────────
    def _row_to_event(self, row: sqlite3.Row) -> EventEnvelope:
        return EventEnvelope(
            id=row["id"], type=row["type"], source=row["source"],
            target=row["target"], aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"], created_at=row["created_at"],
            schema_version=row["schema_version"], priority=row["priority"],
            ttl_seconds=row["ttl_seconds"],
            payload=json.loads(row["payload_json"]),
        )

    def read_since(
        self, seq: int, limit: int = 200,
        types: Optional[Sequence[str]] = None,
    ) -> list[tuple[int, EventEnvelope]]:
        q = "SELECT * FROM events WHERE seq > ?"
        args: list = [int(seq)]
        if types:
            q += " AND type IN (%s)" % ",".join("?" * len(types))
            args.extend(types)
        q += " ORDER BY seq ASC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [(int(r["seq"]), self._row_to_event(r)) for r in rows]

    def read_for(self, aggregate_type: str, aggregate_id: str) -> list[EventEnvelope]:
        """Replay: all events for one aggregate, in order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE aggregate_type=? AND aggregate_id=?"
                " ORDER BY seq ASC",
                (aggregate_type, aggregate_id),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def read_correlation(self, correlation_id: str) -> list[EventEnvelope]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE correlation_id=? ORDER BY seq ASC",
                (correlation_id,),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def last_seq(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT MAX(seq) AS m FROM events").fetchone()
        return int(row["m"] or 0)

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()
        return int(row["c"])

    # ── consumer offsets (at-least-once) ─────────────────────────────────
    def get_offset(self, consumer_name: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_seq FROM consumer_offsets WHERE consumer_name=?",
                (consumer_name,),
            ).fetchone()
        return int(row["last_seq"]) if row else 0

    def commit_offset(self, consumer_name: str, seq: int) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO consumer_offsets (consumer_name, last_seq, updated_at)
                   VALUES (?,?,?)
                   ON CONFLICT(consumer_name)
                   DO UPDATE SET last_seq=excluded.last_seq,
                                 updated_at=excluded.updated_at""",
                (consumer_name, int(seq), _now_iso()),
            )
            self._conn.commit()

    # ── compaction (daily_consolidate sweep) ─────────────────────────────
    def prune_before(self, cutoff_iso: str, archive_path: Optional[str] = None) -> int:
        """Archive-then-delete events older than ``cutoff_iso``.

        Repo policy: never destroy — when ``archive_path`` is given the pruned
        rows are appended there as JSONL before deletion.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE created_at < ? ORDER BY seq ASC",
                (cutoff_iso,),
            ).fetchall()
            if not rows:
                return 0
            if archive_path:
                os.makedirs(os.path.dirname(os.path.abspath(archive_path)), exist_ok=True)
                with open(archive_path, "a", encoding="utf-8") as f:
                    for r in rows:
                        f.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
            self._conn.execute("DELETE FROM events WHERE created_at < ?", (cutoff_iso,))
            self._conn.commit()
            return len(rows)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = ["EventStore"]
