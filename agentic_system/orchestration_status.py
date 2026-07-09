"""Read-only status surface for the orchestration layer.

One command to inspect an unattended/autonomous Hermes setup:

    python -m agentic_system.orchestration_status            # human summary
    python -m agentic_system.orchestration_status --tail 50  # more recent events
    python -m agentic_system.orchestration_status --json     # machine-readable
    python -m agentic_system.orchestration_status --db /path/events.db

Reads ``data/hermes_events.db`` (override via ``HERMES_EVENTS_DB`` /
``orchestration.db_path`` / ``--db``) directly -- no orchestration flag
required, so it works against whatever an autonomous run has written. Safe
to run any time: opens a read-only view, never writes, degrades cleanly on a
missing/empty DB. Intended for headless inspection (after the fact) and for
cron/health-checks (exit code 1 when any breaker is OPEN).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Any, Optional


def _db_path(arg_db: Optional[str]) -> str:
    if arg_db:
        return arg_db
    env = os.getenv("HERMES_EVENTS_DB", "").strip()
    if env:
        return env
    try:
        from agentic_system.events.hooks import events_db_path
        return events_db_path()
    except Exception:
        from pathlib import Path
        repo = Path(__file__).resolve().parents[1]
        return str(repo / "data" / "hermes_events.db")


def _connect(db_path: str) -> Optional[sqlite3.Connection]:
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:
        # table missing on a fresh/partial DB
        return []


def collect(db_path: str, tail: int = 25) -> dict[str, Any]:
    """Return a status snapshot dict, or an ``error`` dict if unreadable."""
    conn = _connect(db_path)
    if conn is None:
        return {"db_path": db_path, "exists": False,
                "note": "no event store at this path yet -- nothing has run"}
    try:
        breakers = _rows(conn, "SELECT * FROM breakers ORDER BY level, key")
        agents = _rows(conn, "SELECT * FROM agent_instances ORDER BY updated_at DESC")
        runs = _rows(conn, "SELECT * FROM workflow_runs ORDER BY updated_at DESC LIMIT 50")
        tasks = _rows(conn, "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT 200")
        task_counts = _rows(conn,
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status ORDER BY n DESC")
        council = _rows(conn,
            "SELECT id, subject_type, subject_ref, status, decision, confidence, "
            "engraphis_ref, created_at FROM council_sessions "
            "ORDER BY created_at DESC LIMIT 20")
        events = _rows(conn,
            "SELECT seq, type, aggregate_type, aggregate_id, correlation_id, "
            "priority, created_at FROM events ORDER BY seq DESC LIMIT ?", (int(tail),))
        event_counts = _rows(conn,
            "SELECT type, COUNT(*) AS n FROM events GROUP BY type ORDER BY n DESC")
        out = {
            "db_path": db_path, "exists": True,
            "breakers": breakers,
            "breaker_any_open": any(b.get("state") == "OPEN" for b in breakers),
            "agents": agents,
            "workflow_runs": runs,
            "task_counts": task_counts,
            "recent_tasks": tasks,
            "council_sessions": council,
            "event_counts": event_counts,
            "recent_events": events,
            "total_events": (lambda r: int(r["c"]) if r else 0)(
                (_rows(conn, "SELECT COUNT(*) AS c FROM events") or [None])[0]
            ),
        }
        return out
    finally:
        conn.close()


def _fmt_human(s: dict[str, Any]) -> str:
    if not s.get("exists"):
        return f"orchestration status: {s.get('note', 'no DB')} ({s.get('db_path')})"

    lines: list[str] = []
    lines.append(f"=== orchestration status  ({s['db_path']}) ===")
    lines.append(f"events: {s['total_events']} total; recent types: "
                 + ", ".join(f"{e['type']}={e['n']}" for e in s["event_counts"][:8])
                 or "events: 0")

    brk = s["breakers"]
    if brk:
        lines.append("")
        lines.append("--- breakers ---")
        for b in brk:
            flag = "  <<< OPEN" if b.get("state") == "OPEN" else ""
            lines.append(f"  {b['level']}/{b['key']}: {b.get('state')}"
                         + (f"  reason={b.get('reason')}" if b.get("reason") else "")
                         + flag)
        if s["breaker_any_open"]:
            lines.append("  ** at least one breaker OPEN -- high-impact tools blocked **")
    else:
        lines.append("\n--- breakers: none recorded (all CLOSED) ---")

    ag = s["agents"]
    lines.append("")
    lines.append(f"--- agents ({len(ag)}) ---")
    for a in ag[:20]:
        hb = a.get("last_heartbeat_at") or "never"
        lines.append(f"  {a['id']} [{a.get('role') or '-'}] {a.get('status')}"
                     f" errs={a.get('error_count', 0)}"
                     f" no_progress={a.get('no_progress_counter', 0)}"
                     f" task={a.get('current_task_id') or '-'} hb={hb}")
    if len(ag) > 20:
        lines.append(f"  ... +{len(ag) - 20} more")

    tc = s["task_counts"]
    lines.append("")
    lines.append("--- tasks ---")
    if tc:
        lines.append("  " + ", ".join(f"{t['status']}={t['n']}" for t in tc))
    else:
        lines.append("  (no tasks)")
    stuck = [t for t in s["recent_tasks"] if t.get("status") in ("ASSIGNED", "WAITING_DEP")]
    for t in stuck[:15]:
        lines.append(f"  {t['status']} {t['id']} type={t['type']}"
                     f" agent={t.get('assigned_agent_id') or '-'}"
                     f" attempts={t.get('attempts', 0)}/{t.get('max_attempts', 3)}"
                     f" updated={t.get('updated_at')}")

    runs = s["workflow_runs"]
    if runs:
        lines.append("")
        lines.append(f"--- workflow runs ({len(runs)} shown) ---")
        for r in runs[:15]:
            lines.append(f"  {r['id']} {r.get('workflow_name')} {r.get('status')}"
                         f" node={r.get('current_node_id') or '-'}"
                         f" updated={r.get('updated_at')}")

    cs = s["council_sessions"]
    if cs:
        lines.append("")
        lines.append(f"--- council sessions ({len(cs)} shown) ---")
        for c in cs[:15]:
            lines.append(f"  {c['id']} {c.get('status')} -> {c.get('decision')}"
                         f" conf={c.get('confidence')}"
                         + (f"  engraphis={c.get('engraphis_ref')}" if c.get("engraphis_ref") else ""))

    ev = s["recent_events"]
    lines.append("")
    lines.append(f"--- recent events (last {len(ev)}) ---")
    for e in ev:
        corr = f" corr={e['correlation_id']}" if e.get("correlation_id") else ""
        agg = f" {e['aggregate_type']}:{e['aggregate_id']}" if e.get("aggregate_type") else ""
        pri = f" [{e['priority']}]" if e.get("priority") and e["priority"] != "normal" else ""
        lines.append(f"  #{e['seq']} {e['type']}{agg}{corr}{pri}  {e.get('created_at')}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agentic_system.orchestration_status",
                                 description="Read-only orchestration status")
    ap.add_argument("--db", help="events DB path (default: orchestration events DB)")
    ap.add_argument("--tail", type=int, default=25, help="recent events to show")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args(argv)
    s = collect(_db_path(args.db), tail=args.tail)
    if "error" in s:
        print(json.dumps(s, ensure_ascii=False, indent=2) if args.json
              else f"error: {s['error']}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2, default=str))
    else:
        print(_fmt_human(s))
    # non-zero exit when a breaker is OPEN -- useful for health checks/cron.
    return 1 if s.get("breaker_any_open") else 0


if __name__ == "__main__":
    raise SystemExit(main())