"""Read-only status / health CLI for the orchestration layer.

Exits non-zero when any breaker is OPEN, so it doubles as a health check.

Usage::

    python -m agentic_system.orchestration_status
    # JSON output:
    python -m agentic_system.orchestration_status --json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from agentic_system.breakers import get_registry
from agentic_system.council.service import CouncilService
from agentic_system.events import get_bus, events_db_path
from agentic_system.ports import get_config_port
from agentic_system.workflow.engine import WorkflowEngine


def collect_status() -> dict[str, Any]:
    """Gather a full read-only status snapshot."""
    config = get_config_port()
    db = events_db_path()
    reg = get_registry(db)
    bus = get_bus(db)

    # Breakers
    breaker_snapshot = reg.snapshot()
    global_open = any(b["level"] == "global" and b["state"] == "OPEN"
                      for b in breaker_snapshot)

    # Council
    council_status = {"configured": False}
    try:
        council = CouncilService(db)
        council_status = {
            "configured": True,
            "members": [m.id for m in council.members],
            "thresholds": council.thresholds.model_dump(),
            "peer_eval": council.peer_eval,
            "min_quorum": council.min_quorum,
        }
    except Exception as e:
        council_status["error"] = str(e)

    # Workflow
    workflow_status = {"registered": []}
    try:
        wf_engine = WorkflowEngine(db)
        workflow_status = {
            "registered": list(wf_engine._defs.keys()),
        }
    except Exception as e:
        workflow_status["error"] = str(e)

    # Event bus health
    bus_health = {"connected": bus is not None}
    if bus:
        try:
            recent = bus.query(limit=1)
            bus_health["recent_events"] = len(recent)
        except Exception as e:
            bus_health["query_error"] = str(e)

    return {
        "orchestration_enabled": config.orchestration_enabled(),
        "events_db": db,
        "global_breaker_open": global_open,
        "breakers": breaker_snapshot,
        "council": council_status,
        "workflows": workflow_status,
        "bus": bus_health,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="agentic-system orchestration status / health check")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    try:
        status = collect_status()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(status, sys.stdout, indent=2)
        print()
    else:
        print("=== agentic-system orchestration status ===")
        print(f"enabled:        {status['orchestration_enabled']}")
        print(f"events_db:      {status['events_db']}")
        print(f"global_open:    {status['global_breaker_open']}")
        print()
        print("--- breakers ---")
        for b in status["breakers"]:
            print(f"  {b['level']:8} {b['key']:20} {b['state']}  ({b['reason']})")
        print()
        print(f"--- council ---")
        c = status["council"]
        if c.get("configured"):
            print(f"  members:      {', '.join(c['members'])}")
            print(f"  thresholds:   {c['thresholds']}")
            print(f"  peer_eval:    {c['peer_eval']}")
            print(f"  min_quorum:   {c['min_quorum']}")
        else:
            print(f"  not configured: {c.get('error')}")
        print()
        print(f"--- workflows ---")
        for wf in status["workflows"].get("registered", []):
            print(f"  {wf}")

    # Exit code: 1 if global breaker OPEN (health check failure)
    return 1 if status["global_breaker_open"] else 0


if __name__ == "__main__":
    sys.exit(main())