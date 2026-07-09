"""Deterministic orchestration sweeps (handoff §3.6) — no LLM involvement.

Four sweeps, designed to run as ``no_agent=True`` script cron jobs (the
scheduler's script runner) or from any process:

=================  ========  =====================================================
sweep              cadence   action
=================  ========  =====================================================
heartbeat_sweep    1 min     stale agent heartbeats -> AgentUnresponsive, CAS
                             their ASSIGNED tasks back to PENDING
stuck_task_sweep   5 min     ASSIGNED/WAITING_DEP tasks past threshold ->
                             requeue (attempts++) or escalate to council/manager
metric_watchdog    10 min    failure ratios + budget exhaustion counts from the
                             event store -> trip agent/global breakers
daily_consolidate  nightly   archive-then-prune old events to _archive/ (repo
                             deletion policy); optionally run engraphis
                             consolidation CLI when installed
=================  ========  =====================================================

CLI:  python -m cron.sweeps <heartbeat|stuck_tasks|metrics|consolidate>
Env:  HERMES_EVENTS_DB overrides the DB path (same as agent.events.hooks).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from agentic_system.breakers import BreakerRegistry, GLOBAL_KEY
from agentic_system.events.bus import EventBus
from agentic_system.events.hooks import events_db_path
from agentic_system.events.state_tables import connect, ensure_state_tables, now_iso
from agentic_system.events.store import EventStore

_HUB_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ARCHIVE_DIR = _HUB_ROOT / "_archive" / "hermes-events"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _cutoff(seconds: float) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))


def _open_all(db_path: Optional[str]):
    db = db_path or events_db_path()
    conn = connect(db)
    ensure_state_tables(conn)
    store = EventStore(db)
    return db, conn, store, EventBus(store, source="sweeps")


# ── 1. heartbeat sweep (1 min) ────────────────────────────────────────────

def heartbeat_sweep(db_path: Optional[str] = None,
                    stale_after_s: float = 120.0) -> dict[str, Any]:
    _, conn, store, bus = _open_all(db_path)
    try:
        cutoff = _cutoff(stale_after_s)
        stale = conn.execute(
            """SELECT id FROM agent_instances
               WHERE status NOT IN ('UNRESPONSIVE','RETIRED')
                 AND (last_heartbeat_at IS NULL OR last_heartbeat_at < ?)""",
            (cutoff,),
        ).fetchall()
        requeued: list[str] = []
        pending_events = []  # published AFTER commit — a second connection
        # writing mid-transaction would deadlock on the SQLite write lock.
        for row in stale:
            agent_id = row["id"]
            conn.execute(
                "UPDATE agent_instances SET status='UNRESPONSIVE', updated_at=? WHERE id=?",
                (now_iso(), agent_id),
            )
            pending_events.append((
                "agent.unresponsive", {"agent_id": agent_id, "cutoff": cutoff},
                dict(aggregate_type="Agent", aggregate_id=agent_id, priority="high"),
            ))
            # CAS the vanished agent's tasks back to the queue
            tasks = conn.execute(
                "SELECT id FROM tasks WHERE assigned_agent_id=? AND status='ASSIGNED'",
                (agent_id,),
            ).fetchall()
            for t in tasks:
                cur = conn.execute(
                    """UPDATE tasks SET status='PENDING', assigned_agent_id=NULL,
                           updated_at=? WHERE id=? AND status='ASSIGNED'""",
                    (now_iso(), t["id"]),
                )
                if cur.rowcount:
                    requeued.append(t["id"])
                    pending_events.append((
                        "task.requeued",
                        {"task_id": t["id"], "reason": "agent_unresponsive",
                         "agent_id": agent_id},
                        dict(aggregate_type="Task", aggregate_id=t["id"]),
                    ))
        conn.commit()
        for etype, payload, kw in pending_events:
            bus.publish(etype, payload, **kw)
        return {"sweep": "heartbeat", "stale_agents": [r["id"] for r in stale],
                "requeued_tasks": requeued}
    finally:
        conn.close(); store.close()


# ── 2. stuck task sweep (5 min) ───────────────────────────────────────────

def stuck_task_sweep(db_path: Optional[str] = None,
                     assigned_stale_s: float = 900.0,
                     waiting_stale_s: float = 3600.0) -> dict[str, Any]:
    _, conn, store, bus = _open_all(db_path)
    try:
        requeued, escalated, failed = [], [], []
        pending_events = []  # published AFTER commit (SQLite write-lock)
        for row in conn.execute(
            "SELECT * FROM tasks WHERE status='ASSIGNED' AND updated_at < ?",
            (_cutoff(assigned_stale_s),),
        ).fetchall():
            attempts = row["attempts"] + 1
            if attempts >= row["max_attempts"]:
                conn.execute(
                    "UPDATE tasks SET status='FAILED', attempts=?, updated_at=? "
                    "WHERE id=? AND status='ASSIGNED'",
                    (attempts, now_iso(), row["id"]),
                )
                failed.append(row["id"])
                pending_events.append((
                    "task.failed",
                    {"task_id": row["id"], "reason": "stuck_retries_exhausted",
                     "attempts": attempts},
                    dict(aggregate_type="Task", aggregate_id=row["id"],
                         correlation_id=row["workflow_run_id"], priority="high"),
                ))
            else:
                conn.execute(
                    """UPDATE tasks SET status='PENDING', assigned_agent_id=NULL,
                           attempts=?, updated_at=? WHERE id=? AND status='ASSIGNED'""",
                    (attempts, now_iso(), row["id"]),
                )
                requeued.append(row["id"])
                pending_events.append((
                    "task.requeued",
                    {"task_id": row["id"], "reason": "assigned_stale",
                     "attempts": attempts},
                    dict(aggregate_type="Task", aggregate_id=row["id"],
                         correlation_id=row["workflow_run_id"]),
                ))
        for row in conn.execute(
            "SELECT * FROM tasks WHERE status='WAITING_DEP' AND updated_at < ?",
            (_cutoff(waiting_stale_s),),
        ).fetchall():
            escalated.append(row["id"])
            pending_events.append((
                "task.escalated",
                {"task_id": row["id"], "reason": "waiting_dep_stale",
                 "node_id": row["node_id"]},
                dict(aggregate_type="Task", aggregate_id=row["id"],
                     correlation_id=row["workflow_run_id"], priority="high"),
            ))
        conn.commit()
        for etype, payload, kw in pending_events:
            bus.publish(etype, payload, **kw)
        return {"sweep": "stuck_tasks", "requeued": requeued,
                "escalated": escalated, "failed": failed}
    finally:
        conn.close(); store.close()


# ── 3. metric watchdog (10 min) ───────────────────────────────────────────

def metric_watchdog(db_path: Optional[str] = None,
                    window_s: float = 600.0,
                    agent_error_threshold: int = 5,
                    global_fail_ratio: float = 0.5,
                    global_min_events: int = 10,
                    budget_exhaustion_threshold: int = 3) -> dict[str, Any]:
    """Trip breakers from event-store metrics. Deterministic, idempotent."""
    db, conn, store, bus = _open_all(db_path)
    breakers = BreakerRegistry(db, bus=bus)
    try:
        cutoff = _cutoff(window_s)
        rows = store._conn.execute(  # read-only aggregate over events
            """SELECT type, aggregate_id, COUNT(*) AS n FROM events
               WHERE created_at >= ? AND type IN
                 ('turn.failed','turn.completed','task.failed','task.completed',
                  'budget.token_exhausted','agent.state_changed')
               GROUP BY type, aggregate_id""",
            (cutoff,),
        ).fetchall()
        per_agent_failures: dict[str, int] = {}
        totals = {"failed": 0, "completed": 0, "budget_exhausted": 0}
        for r in rows:
            t, agg, n = r["type"], r["aggregate_id"] or "?", int(r["n"])
            if t in ("turn.failed", "task.failed"):
                totals["failed"] += n
                per_agent_failures[agg] = per_agent_failures.get(agg, 0) + n
            elif t in ("turn.completed", "task.completed"):
                totals["completed"] += n
            elif t == "budget.token_exhausted":
                totals["budget_exhausted"] += n

        tripped = []
        for agent_id, n in per_agent_failures.items():
            if n >= agent_error_threshold and not breakers.is_open("agent", agent_id):
                breakers.open("agent", agent_id,
                              f"{n} failures in {int(window_s)}s window")
                tripped.append(("agent", agent_id))

        total = totals["failed"] + totals["completed"]
        ratio = (totals["failed"] / total) if total else 0.0
        if (total >= global_min_events and ratio >= global_fail_ratio) or \
           totals["budget_exhausted"] >= budget_exhaustion_threshold:
            if not breakers.is_open("global", GLOBAL_KEY):
                breakers.open("global", GLOBAL_KEY,
                              f"fail_ratio={ratio:.2f} over {total} events, "
                              f"budget_exhaustions={totals['budget_exhausted']} "
                              f"in {int(window_s)}s")
                tripped.append(("global", GLOBAL_KEY))
        return {"sweep": "metrics", "window_s": window_s, "totals": totals,
                "fail_ratio": round(ratio, 3), "tripped": tripped}
    finally:
        breakers.close_conn(); conn.close(); store.close()


# ── 4. daily consolidate (nightly) ────────────────────────────────────────

def daily_consolidate(db_path: Optional[str] = None,
                      retain_days: float = 14.0,
                      archive_dir: Optional[str] = None,
                      run_engraphis: bool = True) -> dict[str, Any]:
    db, conn, store, _ = _open_all(db_path)
    try:
        adir = Path(archive_dir) if archive_dir else _DEFAULT_ARCHIVE_DIR
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        archive = adir / f"events-pruned-{stamp}.jsonl"
        cutoff = _cutoff(retain_days * 86400)
        pruned = store.prune_before(cutoff, archive_path=str(archive))

        engraphis_result = "skipped"
        if run_engraphis:
            exe = shutil.which("engraphis-consolidate")
            if exe:
                try:
                    proc = subprocess.run([exe], capture_output=True, text=True,
                                          timeout=600)
                    engraphis_result = f"exit={proc.returncode}"
                except Exception as exc:  # never fail the sweep on engraphis
                    engraphis_result = f"error={type(exc).__name__}"
            else:
                engraphis_result = "cli_not_found"
        return {"sweep": "consolidate", "pruned_events": pruned,
                "archive": str(archive) if pruned else None,
                "engraphis": engraphis_result}
    finally:
        conn.close(); store.close()


# ── CLI ───────────────────────────────────────────────────────────────────

_SWEEPS = {
    "heartbeat": heartbeat_sweep,
    "stuck_tasks": stuck_task_sweep,
    "metrics": metric_watchdog,
    "consolidate": daily_consolidate,
}

# schedule expressions for ensure_sweep_jobs / manual cron registration
SWEEP_SCHEDULES = {
    "heartbeat": "*/1 * * * *",
    "stuck_tasks": "*/5 * * * *",
    "metrics": "*/10 * * * *",
    "consolidate": "0 3 * * *",
}


def register_sweeps(dry_run: bool = False) -> dict[str, Any]:
    """Idempotently register the four orchestration sweeps as cron jobs via
    the host's CronPort.

    Writes a small ``.py`` wrapper per sweep into the CronPort's ``scripts_dir``
    and creates the job via ``create_job``. Existing jobs with the same name
    are left untouched (wrappers still refreshed), so this is safe to re-run.
    Each wrapper bakes in this package's repo root so ``agentic_system.sweeps``
    stays importable regardless of how the host installed it.

    Requires a CronPort to be registered (``set_cron_port``). ``dry_run=True``
    reports what would be written/created without touching the filesystem.
    """
    from agentic_system.ports import get_cron_port
    try:
        cron = get_cron_port()
    except Exception as e:
        return {"ok": False, "error": f"no CronPort registered: {e}"}
    scripts_dir = Path(cron.scripts_dir())
    existing = set(cron.list_job_names())
    # repo root = the directory containing this package (one level up).
    repo_root = Path(__file__).resolve().parents[1]
    created, skipped, scripts = [], [], []
    for sweep, sched in SWEEP_SCHEDULES.items():
        name = f"sweep-{sweep}"
        script_name = f"{name}.py"
        script_path = scripts_dir / script_name
        wrapper = (
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"sys.path.insert(0, r'{repo_root}')\n"
            "from agentic_system.sweeps import main\n"
            f"raise SystemExit(main([{sweep!r}]))\n"
        )
        if name in existing:
            skipped.append(name)
            if not dry_run:
                scripts_dir.mkdir(parents=True, exist_ok=True)
                script_path.write_text(wrapper, encoding="utf-8")
            continue
        scripts.append(str(script_path))
        if dry_run:
            continue
        scripts_dir.mkdir(parents=True, exist_ok=True)
        script_path.write_text(wrapper, encoding="utf-8")
        cron.create_job(name=name, schedule=sched, script=script_name,
                        workdir=str(repo_root))
        created.append(name)
    return {"ok": True, "created": created, "skipped": skipped,
            "scripts": scripts, "dry_run": dry_run,
            "schedules": dict(SWEEP_SCHEDULES)}


def main(argv: Optional[list[str]] = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] not in _SWEEPS:
        if args and args[0] == "register":
            print(json.dumps(register_sweeps(), ensure_ascii=False, indent=2))
            return 0
        print(f"usage: python -m agentic_system.sweeps <{'|'.join(_SWEEPS)}|register>", file=sys.stderr)
        return 2
    result = _SWEEPS[args[0]]()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
