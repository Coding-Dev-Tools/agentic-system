"""Periodic sweeps: heartbeat / stuck-task recovery / metric watchdog / nightly consolidate.

Register with the host's CronPort at startup::

    from agentic_system.sweeps import register_sweeps
    register_sweeps()

The host CronPort provides ``scripts_dir()``, ``list_job_names()``, and
``create_job(name, schedule, script, workdir)``. Each sweep is a small
script (shipped in the package) invoked on a cron schedule.
"""

from __future__ import annotations

import importlib.resources
import logging
import textwrap
from pathlib import Path
from typing import Any

from agentic_system.ports import get_config_port, get_cron_port

logger = logging.getLogger("agentic_system.sweeps")

# ── Sweep definitions ────────────────────────────────────────────────────

SWEEPS = {
    "heartbeat": {
        "schedule": "*/5 * * * *",          # every 5 minutes
        "description": "verify agent liveness + write work-log entry",
        "script": "heartbeat.py",
    },
    "stuck_task_recovery": {
        "schedule": "*/10 * * * *",         # every 10 minutes
        "description": "re-queue workflow tasks stuck > threshold",
        "script": "stuck_task_recovery.py",
    },
    "metric_watchdog": {
        "schedule": "*/5 * * * *",          # every 5 minutes
        "description": "alert on breaker OPEN, queue depth, error rates",
        "script": "metric_watchdog.py",
    },
    "nightly_consolidate": {
        "schedule": "0 3 * * *",            # 3 AM daily
        "description": "prune old events, archive to JSONL, vacuum DB",
        "script": "nightly_consolidate.py",
    },
}

# ── Built-in sweep scripts (embedded; written to scripts_dir on first register) ──

HEARTBEAT_SCRIPT = textwrap.dedent("""\
    #!/usr/bin/env python3
    \"\"\"Heartbeat sweep: verify agent liveness + write work-log entry.\"\"\"
    import sys, json, os, datetime, sqlite3
    from pathlib import Path

    HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    DB = HERMES_HOME / "state.db"

    def main():
        now = datetime.datetime.utcnow().isoformat() + "Z"
        ok = True
        details = {}
        # 1. Check state.db has recent agent activity
        try:
            conn = sqlite3.connect(DB)
            row = conn.execute(
                "SELECT MAX(ts) FROM events WHERE type LIKE 'agent.%'"
            ).fetchone()
            if row and row[0]:
                from datetime import datetime
                last = datetime.fromisoformat(row[0].replace('Z', '+00:00'))
                delta = (datetime.utcnow() - last).total_seconds()
                details["last_agent_event_sec"] = delta
                if delta > 300:  # 5 min
                    ok = False
                    details["reason"] = "no agent activity > 5 min"
        except Exception as e:
            ok = False
            details["db_error"] = str(e)
        # 2. Write work-log
        log = HERMES_HOME / "cron" / "work-log.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps({
            "ts": now, "job": "heartbeat",
            "did": "heartbeat check", "outcome": "ok" if ok else "degraded",
            "next": "continue", **details
        }) + "\\n", encoding="utf-8")
        sys.exit(0 if ok else 1)

    if __name__ == "__main__": main()
""")

STUCK_TASK_RECOVERY_SCRIPT = textwrap.dedent("""\
    #!/usr/bin/env python3
    \"\"\"Stuck-task recovery: find workflows/tasks stuck > threshold and re-queue.\"\"\"
    import sys, os, json, sqlite3, datetime
    from pathlib import Path

    HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    DB = HERMES_HOME / "state.db"
    THRESHOLD_MIN = int(os.environ.get("STUCK_THRESHOLD_MIN", "30"))

    def main():
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        now = datetime.datetime.utcnow()
        recovered = 0
        rows = conn.execute(
            "SELECT instance_id, workflow_name, claimed_by, claim_ts "
            "FROM workflow_instances WHERE state='RUNNING' AND claim_ts IS NOT NULL"
        ).fetchall()
        for r in rows:
            claim_ts = datetime.datetime.fromisoformat(r["claim_ts"].replace('Z', '+00:00'))
            delta = (now - claim_ts).total_seconds() / 60
            if delta > THRESHOLD_MIN:
                conn.execute(
                    "UPDATE workflow_instances SET state='PENDING', claimed_by=NULL, claim_ts=NULL, "
                    "updated_at=? WHERE instance_id=?",
                    (datetime.datetime.utcnow().isoformat() + "Z", r["instance_id"]))
                conn.execute(
                    "UPDATE workflow_claims SET outcome='TIMEOUT', result_json='{}' "
                    "WHERE instance_id=? AND claimed_by=?",
                    (r["instance_id"], r["claimed_by"]))
                recovered += 1
                print(f"Recovered {r['instance_id']} (stuck {delta:.1f} min)")
        conn.commit()
        log = HERMES_HOME / "cron" / "work-log.jsonl"
        log.write_text(json.dumps({
            "ts": now.isoformat() + "Z", "job": "stuck_task_recovery",
            "did": f"recovered {recovered} stuck workflows", "outcome": "ok",
            "next": "continue"
        }) + "\\n", encoding="utf-8")
        print(f"Recovered {recovered} tasks")

    if __name__ == "__main__": main()
""")

METRIC_WATCHDOG_SCRIPT = textwrap.dedent("""\
    #!/usr/bin/env python3
    \"\"\"Metric watchdog: alert on breaker OPEN, queue depth, error rates.\"\"\"
    import sys, os, json, sqlite3, datetime
    from pathlib import Path

    HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    DB = HERMES_HOME / "state.db"

    def main():
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        alerts = []
        # 1. Global breaker OPEN?
        row = conn.execute(
            "SELECT 1 FROM breakers WHERE level='global' AND key='system' AND state='OPEN'"
        ).fetchone()
        if row:
            alerts.append("GLOBAL_CIRCUIT_BREAKER_OPEN")
        # 2. High-priority queue depth (events with priority='high' in last 5 min)
        since = (datetime.datetime.utcnow() - datetime.timedelta(minutes=5)).isoformat() + "Z"
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE priority IN ('high','critical') AND ts >= ?",
            (since,)).fetchone()
        if row and row[0] > 50:
            alerts.append(f"HIGH_PRIORITY_EVENT_SPIKE:{row[0]}")
        # 3. Error rate
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE type LIKE '%error%' AND ts >= ?",
            (since,)).fetchone()
        if row and row[0] > 10:
            alerts.append(f"ERROR_RATE_SPIKE:{row[0]}")

        if alerts:
            alert_file = HERMES_HOME / "ALERT.md"
            with open(alert_file, "a", encoding="utf-8") as f:
                for a in alerts:
                    f.write(f"\\n[{datetime.datetime.utcnow().isoformat()}Z] WATCHDOG: {a}\\n")
        print(json.dumps({"alerts": alerts}))

    if __name__ == "__main__": main()
""")

NIGHTLY_CONSOLIDATE_SCRIPT = textwrap.dedent("""\
    #!/usr/bin/env python3
    \"\"\"Nightly consolidate: prune old events, archive to JSONL, vacuum DB.\"\"\"
    import sys, os, json, sqlite3, datetime, shutil
    from pathlib import Path

    HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    DB = HERMES_HOME / "state.db"
    ARCHIVE_DIR = HERMES_HOME / "archive"
    RETENTION_DAYS = int(os.environ.get("EVENT_RETENTION_DAYS", "30"))

    def main():
        ARCHIVE_DIR.mkdir(exist_ok=True)
        today = datetime.datetime.utcnow().date()
        cutoff = (today - datetime.timedelta(days=RETENTION_DAYS)).isoformat()
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        # Archive old events
        rows = conn.execute(
            "SELECT * FROM events WHERE ts < ? ORDER BY ts", (cutoff,)).fetchall()
        if rows:
            archive_file = ARCHIVE_DIR / f"events_{cutoff}.jsonl"
            with open(archive_file, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(dict(r)) + "\\n")
            # Delete archived
            conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            conn.commit()
            print(f"Archived {len(rows)} events to {archive_file}")
        # Vacuum
        conn.execute("VACUUM")
        conn.close()

    if __name__ == "__main__": main()
""")

SCRIPTS = {
    "heartbeat.py": HEARTBEAT_SCRIPT,
    "stuck_task_recovery.py": STUCK_TASK_RECOVERY_SCRIPT,
    "metric_watchdog.py": METRIC_WATCHDOG_SCRIPT,
    "nightly_consolidate.py": NIGHTLY_CONSOLIDATE_SCRIPT,
}


def register_sweeps() -> None:
    """Write sweep scripts to host's scripts_dir and register via CronPort."""
    cron = get_cron_port()
    scripts_dir = Path(cron.scripts_dir())
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # Write scripts
    for name, content in SCRIPTS.items():
        path = scripts_dir / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            # Make executable on Unix
            try:
                path.chmod(0o755)
            except Exception:
                pass

    # Register cron jobs
    existing = set(cron.list_job_names())
    for name, cfg in SWEEPS.items():
        if name not in existing:
            cron.create_job(
                name=name,
                schedule=cfg["schedule"],
                script=cfg["script"],
                workdir=str(scripts_dir),
            )
            logger.info("registered sweep: %s (%s)", name, cfg["schedule"])


def unregister_sweeps() -> None:
    """Remove sweep cron jobs (not implemented - CronPort has no delete)."""
    pass