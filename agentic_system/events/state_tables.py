"""Shared materialized-view tables in the events DB.

The events table is the system of record; these tables are projections kept
current by their owners (workflow engine, breaker registry, sweeps, council).
All phases call :func:`ensure_state_tables` idempotently.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

_DDL = """
CREATE TABLE IF NOT EXISTS agent_instances (
    id                  TEXT PRIMARY KEY,
    role                TEXT DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'IDLE',
    current_task_id     TEXT,
    last_heartbeat_at   TEXT,
    error_count         INTEGER NOT NULL DEFAULT 0,
    no_progress_counter INTEGER NOT NULL DEFAULT 0,
    config_json         TEXT NOT NULL DEFAULT '{}',
    updated_at          TEXT
);
CREATE TABLE IF NOT EXISTS workflow_runs (
    id              TEXT PRIMARY KEY,
    workflow_name   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'RUNNING',
    current_node_id TEXT,
    context_json    TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT,
    updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    type              TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'PENDING',
    workflow_run_id   TEXT,
    node_id           TEXT,
    input_ref         TEXT,
    output_ref        TEXT,
    assigned_agent_id TEXT,
    dependencies_json TEXT NOT NULL DEFAULT '[]',
    attempts          INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL DEFAULT 3,
    priority          TEXT NOT NULL DEFAULT 'normal',
    created_at        TEXT,
    updated_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_agent  ON tasks(assigned_agent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_run    ON tasks(workflow_run_id);
CREATE TABLE IF NOT EXISTS council_sessions (
    id            TEXT PRIMARY KEY,
    subject_type  TEXT,
    subject_ref   TEXT,
    subject_hash  TEXT,
    rubric_hash   TEXT,
    status        TEXT NOT NULL DEFAULT 'PENDING',
    decision      TEXT,
    confidence    REAL,
    session_json  TEXT NOT NULL DEFAULT '{}',
    engraphis_ref TEXT,
    created_at    TEXT,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_council_cache ON council_sessions(subject_hash, rubric_hash);
CREATE TABLE IF NOT EXISTS breakers (
    level        TEXT NOT NULL,
    key          TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'CLOSED',
    reason       TEXT,
    opened_at    TEXT,
    half_open_at TEXT,
    updated_at   TEXT,
    PRIMARY KEY (level, key)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def connect(db_path: str, timeout: float = 30.0) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
    return conn


def ensure_state_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()


def heartbeat(conn: sqlite3.Connection, agent_id: str, role: str = "",
              status: Optional[str] = None) -> None:
    """Workers call this periodically; heartbeat_sweep reads it."""
    ts = now_iso()
    conn.execute(
        """INSERT INTO agent_instances (id, role, status, last_heartbeat_at, updated_at)
           VALUES (?,?,COALESCE(?, 'IDLE'),?,?)
           ON CONFLICT(id) DO UPDATE SET
             last_heartbeat_at=excluded.last_heartbeat_at,
             updated_at=excluded.updated_at,
             role=CASE WHEN excluded.role != '' THEN excluded.role ELSE agent_instances.role END,
             status=COALESCE(?, agent_instances.status)""",
        (agent_id, role, status, ts, ts, status),
    )
    conn.commit()


__all__ = ["connect", "ensure_state_tables", "heartbeat", "now_iso"]
