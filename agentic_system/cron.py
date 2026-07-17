"""Cron scheduler + persistent jobs (from Hermes).

Host implements CronPort; this module provides a reference implementation
using APScheduler + SQLite job store for persistence across restarts.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

from agentic_system.ports import get_config_port
from agentic_system.events import connect, ensure_state_tables, now_iso

logger = logging.getLogger("agentic_system.cron")


@dataclass
class CronJob:
    name: str
    schedule: str              # cron expression
    script: str                # script filename (relative to scripts_dir)
    workdir: str               # working directory
    enabled: bool = True
    last_run_at: Optional[str] = None
    next_run_at: Optional[str] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None


class SQLiteCronPort:
    """Reference CronPort implementation using SQLite + APScheduler.

    Usage::
        from agentic_system.cron import SQLiteCronPort
        cron = SQLiteCronPort(db_path)
        cron.create_job(name="my-job", schedule="*/5 * * * *",
                        script="my_script.py", workdir="/path/to/scripts")
    """

    def __init__(self, db_path: str, scripts_dir: Optional[str] = None):
        self.db_path = db_path
        self._scripts_dir = Path(scripts_dir or os.environ.get("CRON_SCRIPTS_DIR",
                             Path.home() / ".hermes" / "cron" / "scripts"))
        self._conn = connect(db_path)
        ensure_state_tables(self._conn)
        self._scheduler = None
        self._lock = threading.Lock()

    def scripts_dir(self) -> str:
        return str(self._scripts_dir)

    def list_job_names(self) -> Sequence[str]:
        rows = self._conn.execute("SELECT name FROM cron_jobs").fetchall()
        return [r["name"] for r in rows]

    def create_job(self, *, name: str, schedule: str, script: str,
                   workdir: str) -> None:
        ts = now_iso()
        self._conn.execute(
            """INSERT INTO cron_jobs
               (name, schedule, script, workdir, enabled, created_at, updated_at)
               VALUES (?,?,?,?,1,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 schedule=excluded.schedule, script=excluded.script,
                 workdir=excluded.workdir, enabled=excluded.enabled,
                 updated_at=excluded.updated_at""",
            (name, schedule, script, workdir, ts, ts),
        )
        self._conn.commit()
        self._reschedule(name)

    def enable_job(self, name: str) -> None:
        self._conn.execute(
            "UPDATE cron_jobs SET enabled=1, updated_at=? WHERE name=?",
            (now_iso(), name))
        self._conn.commit()
        self._reschedule(name)

    def disable_job(self, name: str) -> None:
        self._conn.execute(
            "UPDATE cron_jobs SET enabled=0, updated_at=? WHERE name=?",
            (now_iso(), name))
        self._conn.commit()
        self._unschedule(name)

    def get_job(self, name: str) -> Optional[CronJob]:
        row = self._conn.execute(
            "SELECT * FROM cron_jobs WHERE name=?", (name,)).fetchone()
        if not row:
            return None
        return CronJob(
            name=row["name"], schedule=row["schedule"], script=row["script"],
            workdir=row["workdir"], enabled=bool(row["enabled"]),
            last_run_at=row["last_run_at"], next_run_at=row["next_run_at"],
            last_status=row["last_status"], last_error=row["last_error"],
        )

    def start(self) -> None:
        """Start the background scheduler thread."""
        if self._scheduler is not None:
            return
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
        except ImportError:
            logger.warning("apscheduler not installed; cron scheduler disabled")
            return

        self._scheduler = BackgroundScheduler()
        self._scheduler.start()
        # Load existing jobs
        for row in self._conn.execute(
            "SELECT * FROM cron_jobs WHERE enabled=1").fetchall():
            self._schedule_job(row["name"], row["schedule"], row["script"], row["workdir"])
        logger.info("cron scheduler started with %d jobs", len(self._scheduler.get_jobs()))

    def stop(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown(wait=True)
            self._scheduler = None
        logger.info("cron scheduler stopped")

    def _schedule_job(self, name: str, schedule: str, script: str, workdir: str) -> None:
        if not self._scheduler:
            return
        try:
            from apscheduler.triggers.cron import CronTrigger
            self._scheduler.add_job(
                self._run_job, CronTrigger.from_crontab(schedule),
                args=[name, script, workdir], id=name, replace_existing=True,
                max_instances=1, coalesce=True,
            )
        except Exception as e:
            logger.exception("failed to schedule job %s: %s", name, e)

    def _reschedule(self, name: str) -> None:
        job = self.get_job(name)
        if job and job.enabled:
            self._schedule_job(name, job.schedule, job.script, job.workdir)
        else:
            self._unschedule(name)

    def _unschedule(self, name: str) -> None:
        if self._scheduler:
            try:
                self._scheduler.remove_job(name)
            except Exception:
                pass

    def _run_job(self, name: str, script: str, workdir: str) -> None:
        ts = now_iso()
        logger.info("cron job %s starting", name)
        self._conn.execute(
            "UPDATE cron_jobs SET last_run_at=?, last_status='running', updated_at=? WHERE name=?",
            (ts, ts, name))
        self._conn.commit()

        script_path = self._scripts_dir / script
        if not script_path.exists():
            err = f"script not found: {script_path}"
            logger.error(err)
            self._conn.execute(
                "UPDATE cron_jobs SET last_status='error', last_error=?, updated_at=? WHERE name=?",
                (err, now_iso(), name))
            self._conn.commit()
            return

        try:
            # Run script with timeout
            result = subprocess.run(
                [sys.executable, str(script_path)],
                cwd=workdir, capture_output=True, text=True, timeout=300,
            )
            status = "ok" if result.returncode == 0 else "error"
            error = result.stderr if result.returncode != 0 else None
        except subprocess.TimeoutExpired:
            status = "timeout"
            error = "job timed out after 5 minutes"
        except Exception as e:
            status = "error"
            error = str(e)

        self._conn.execute(
            """UPDATE cron_jobs SET last_status=?, last_error=?, updated_at=?
               WHERE name=?""",
            (status, error, now_iso(), name))
        self._conn.commit()
        logger.info("cron job %s finished: %s", name, status)


class InMemoryCronPort:
    """Test double for CronPort."""

    def __init__(self):
        self._jobs: dict[str, CronJob] = {}
        self._scripts_dir = Path.cwd()

    def scripts_dir(self) -> str:
        return str(self._scripts_dir)

    def list_job_names(self) -> Sequence[str]:
        return list(self._jobs.keys())

    def create_job(self, *, name: str, schedule: str, script: str,
                   workdir: str) -> None:
        self._jobs[name] = CronJob(name=name, schedule=schedule,
                                   script=script, workdir=workdir)

    def enable_job(self, name: str) -> None:
        if name in self._jobs:
            self._jobs[name].enabled = True

    def disable_job(self, name: str) -> None:
        if name in self._jobs:
            self._jobs[name].enabled = False

    def get_job(self, name: str) -> Optional[CronJob]:
        return self._jobs.get(name)