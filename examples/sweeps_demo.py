#!/usr/bin/env python3
"""Example: Periodic sweeps — heartbeat, stuck-task recovery, metric watchdog, nightly consolidate."""

import os
import tempfile
import time
from agentic_system.sweeps import register_sweeps, get_sweep_scripts_dir, SCRIPTS
from agentic_system.ports import get_config_port, set_config_port, set_token_budget_port, set_default_llm_fn
from agentic_system.cron import SQLiteCronPort


# ── Minimal host ports ─────────────────────────────────────────────────────
class _Config:
    def orchestration_enabled(self): return True
    def events_db_path(self): return "/tmp/sweeps_demo.db"
    def council_config(self): return None
    def state_tool_policy(self): return None

class _Budget:
    def make(self, max_tokens):
        class B:
            def __init__(s): s.max_total, s.used, s.exceeded = max_tokens, 0, False
            def consume(s, t): s.used += t; s.exceeded = s.used >= s.max_total
        return B(max_tokens)

set_config_port(_Config())
set_token_budget_port(_Budget())
set_default_llm_fn(lambda m, sys, usr: '{}')


def demo_register_sweeps():
    """Register sweep scripts and cron jobs."""
    print("=== Register Sweeps ===")
    register_sweeps()
    scripts_dir = get_sweep_scripts_dir()
    print(f"Scripts written to: {scripts_dir}")
    for name in SCRIPTS:
        path = scripts_dir / SCRIPTS[name]["script"]
        print(f"  ✅ {name} -> {path}")
    print()


def demo_run_sweep_directly():
    """Run a sweep script directly (for testing)."""
    print("=== Run Heartbeat Sweep Directly ===")
    import subprocess
    script = get_sweep_scripts_dir() / "heartbeat.py"
    result = subprocess.run(["python", str(script)], capture_output=True, text=True)
    print(f"Exit code: {result.returncode}")
    print(f"stdout: {result.stdout[:200]}")
    if result.stderr:
        print(f"stderr: {result.stderr[:200]}")
    print()


def demo_cron_port():
    """Show SQLiteCronPort managing persistent jobs."""
    print("=== SQLiteCronPort ===")
    cron = SQLiteCronPort("/tmp/cron_demo.db", "/tmp/cron_scripts")
    cron.create_job(
        name="my_job",
        schedule="*/10 * * * *",
        script="echo hello",
        workdir="/tmp",
    )
    cron.create_job(
        name="disabled_job",
        schedule="0 * * * *",
        script="echo never runs",
        workdir="/tmp",
    )
    cron.disable_job("disabled_job")

    print("Registered jobs:")
    for name in cron.list_job_names():
        job = cron.get_job(name)
        print(f"  {name}: {job.schedule} | enabled={job.enabled} | script={job.script}")

    print("\nJob details:")
    for name in cron.list_job_names():
        job = cron.get_job(name)
        print(f"  {name}: last_run={job.last_run_at}, next_run={job.next_run_at}, status={job.last_status}")
    print()


def demo_sweep_scripts_content():
    """Show what the embedded sweep scripts do."""
    print("=== Sweep Script Summaries ===")
    for name, cfg in SCRIPTS.items():
        print(f"\n{name} ({cfg['schedule']}): {cfg['description']}")
        print(f"  Script: {cfg['script']}")
    print()


def main():
    demo_register_sweeps()
    demo_run_sweep_directly()
    demo_cron_port()
    demo_sweep_scripts_content()


if __name__ == "__main__":
    main()