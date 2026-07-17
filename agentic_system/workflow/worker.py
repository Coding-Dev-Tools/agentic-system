"""Workflow worker: background process that polls, claims, executes, advances.

Run as a separate process (systemd, PM2, Windows service) or as a thread.
Integrates with the host's token budget and circuit breakers.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional

from agentic_system.workflow.engine import WorkflowEngine, WorkflowWorker

logger = logging.getLogger("agentic_system.workflow.worker")


@dataclass
class WorkerConfig:
    worker_id: str
    poll_interval: float = 5.0
    max_parallel: int = 4
    shutdown_timeout: float = 30.0


class WorkflowRunner:
    """High-level workflow runner with multiple workers."""

    def __init__(self, engine: WorkflowEngine, config: WorkerConfig):
        self.engine = engine
        self.config = config
        self._workers: list[WorkflowWorker] = []
        self._executor = ThreadPoolExecutor(max_workers=config.max_parallel)
        self._stop = threading.Event()

    def start(self) -> None:
        for i in range(self.config.max_parallel):
            worker = WorkflowWorker(
                self.engine,
                f"{self.config.worker_id}-{i}",
                self.config.poll_interval,
            )
            worker.start()
            self._workers.append(worker)
        logger.info("workflow runner started with %d workers", len(self._workers))

    def stop(self) -> None:
        self._stop.set()
        for w in self._workers:
            w.stop(self.config.shutdown_timeout)
        self._executor.shutdown(wait=True)
        logger.info("workflow runner stopped")

    def submit_task(self, instance_id: str, task_name: str,
                    fn: callable, *args, **kwargs) -> Any:
        """Submit a task for execution (host use)."""
        return self._executor.submit(fn, *args, **kwargs)


def run_worker_cli(engine: WorkflowEngine, worker_id: str,
                   poll_interval: float = 5.0) -> int:
    """CLI entry point for `python -m agentic_system.workflow.worker`."""
    worker = WorkflowWorker(engine, worker_id, poll_interval)
    stop_event = threading.Event()

    def signal_handler(signum, frame):
        logger.info("signal %d received, stopping...", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    worker.start()
    try:
        while not stop_event.is_set():
            stop_event.wait(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
    return 0


if __name__ == "__main__":
    # Example: python -m agentic_system.workflow.worker <worker_id>
    from agentic_system.ports import get_config_port
    db = get_config_port().events_db_path()
    engine = WorkflowEngine(db)
    sys.exit(run_worker_cli(engine, sys.argv[1] if len(sys.argv) > 1 else "worker-0"))