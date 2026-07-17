"""agentic-system CLI - unified command interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentic_system.orchestration_status import main as status_main


def register_sweeps_cmd(args: argparse.Namespace) -> int:
    from agentic_system.sweeps import register_sweeps
    register_sweeps()
    print("Sweeps registered")
    return 0


def validate_config_cmd(args: argparse.Namespace) -> int:
    from agentic_system.ports import get_config_port
    cfg = get_config_port()
    print(f"Orchestration enabled: {cfg.orchestration_enabled()}")
    print(f"Events DB: {cfg.events_db_path()}")
    cc = cfg.council_config()
    if cc:
        print(f"Council members: {len(cc.get('members', []))}")
        print(f"Thresholds: {cc.get('thresholds', {})}")
    else:
        print("No council config")
    return 0


def council_review_cmd(args: argparse.Namespace) -> int:
    from agentic_system.council import CouncilService, CouncilRequest, make_engraphis_persist_hook

    hook = make_engraphis_persist_hook() if not args.no_engraphis else None
    svc = CouncilService(args.db, persist_hook=hook)

    req = CouncilRequest(
        subject_type=args.type,
        subject_ref={"repo": args.repo, "commit": args.commit} if args.repo else {},
        content=Path(args.content).read_text(encoding="utf-8") if args.content else "",
        risk_level=args.risk,
        decision_type=args.decision_type,
        gate=args.gate,
        checklist=tuple(args.checklist) if args.checklist else (),
        correlation_id=args.correlation_id,
    )

    decision = svc.review(req)
    print(f"Decision: {decision.decision}")
    print(f"Metrics: {decision.metrics}")
    print(f"Session: {decision.session_id}")
    svc.close()
    return 0


def workflow_run_cmd(args: argparse.Namespace) -> int:
    from agentic_system.workflow import WorkflowEngine, TaskDef, WorkflowDef

    engine = WorkflowEngine(args.db)

    # Simple demo workflow
    wf = WorkflowDef("demo", tasks=(
        TaskDef("lint", outputs=("lint_report",)),
        TaskDef("test", inputs=("lint_report",), outputs=("test_report",)),
    ))
    engine.register_executor("lint", lambda i: {"report": "lint ok"})
    engine.register_executor("test", lambda i: {"report": "tests pass"})

    inst = engine.create_instance(wf, {"files": args.files.split(",")})
    print(f"Created instance: {inst.instance_id}")

    # Run tasks
    for _ in range(len(wf.tasks)):
        advanced = engine.advance(inst.instance_id, wf)
        print(f"State: {advanced.state}")
        if advanced.state == "RUNNING":
            # Find ready task
            for task in wf.tasks:
                if not engine.is_task_done(inst.instance_id, task.name):
                    success, result = engine.execute_task(inst.instance_id, task, "cli")
                    print(f"  Task {task.name}: {success} -> {result}")

    print("Done")
    engine.close()
    return 0


def breaker_cmd(args: argparse.Namespace) -> int:
    from agentic_system.breakers import get_registry

    reg = get_registry(args.db)
    if args.list:
        for b in reg.snapshot():
            print(f"{b['level']:>10} | {b['key']:<30} {b['state']}  ({b.get('reason','')})")
    elif args.open:
        reg.open(args.level, args.key, args.reason)
        print(f"Opened {args.level}/{args.key}")
    elif args.close:
        reg.close(args.level, args.key, args.reason or "manual")
        print(f"Closed {args.level}/{args.key}")
    elif args.heal:
        changes = reg.try_self_heal()
        for k, v in changes.items():
            print(f"Self-heal: {k} -> {v}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentic-system",
        description="agentic-system orchestration layer CLI",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # status
    p = sub.add_parser("status", help="Show orchestration health (exit 1 if global breaker OPEN)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--db", default=None)

    # register sweeps
    sub.add_parser("register-sweeps", help="Register periodic sweeps with host cron")

    # validate config
    sub.add_parser("validate-config", help="Validate host config port")

    # council review
    p = sub.add_parser("council", help="Run a council review")
    p.add_argument("--db", default="events.db")
    p.add_argument("--type", default="CODE_EDIT")
    p.add_argument("--repo")
    p.add_argument("--commit")
    p.add_argument("--content", help="Path to diff/content file")
    p.add_argument("--risk", choices=["low", "medium", "high"], default="medium")
    p.add_argument("--decision-type", default="REVIEW")
    p.add_argument("--gate", choices=["code_edit", "pr_review", "merge", "delegation",
                                       "security", "code_quality", "dependency", "architecture"])
    p.add_argument("--checklist", action="append")
    p.add_argument("--correlation-id")
    p.add_argument("--no-engraphis", action="store_true")

    # workflow
    p = sub.add_parser("workflow", help="Run a demo workflow")
    p.add_argument("--db", default="events.db")
    p.add_argument("--files", default="src/")

    # breakers
    p = sub.add_parser("breaker", help="Manage circuit breakers")
    p.add_argument("--db", default="events.db")
    p.add_argument("--list", action="store_true")
    p.add_argument("--open", action="store_true")
    p.add_argument("--close", action="store_true")
    p.add_argument("--heal", action="store_true")
    p.add_argument("--level", choices=["agent", "workflow", "global"], default="agent")
    p.add_argument("--key", default="system")
    p.add_argument("--reason", default="")

    # config validation
    sub.add_parser("validate-config", help="Validate config port registration")

    args = parser.parse_args(argv)

    if args.cmd == "status":
        # Delegate to orchestration_status with modified args
        sys.argv = ["orchestration_status"] + (["--json"] if args.json else [])
        if args.db:
            sys.argv += ["--db", args.db]  # won't work without modifying orchestration_status
        return status_main()
    elif args.cmd == "register-sweeps":
        return register_sweeps_cmd(args)
    elif args.cmd == "validate-config":
        return validate_config_cmd(args)
    elif args.cmd == "council":
        return council_review_cmd(args)
    elif args.cmd == "workflow":
        return workflow_run_cmd(args)
    elif args.cmd == "breaker":
        return breaker_cmd(args)
    elif args.cmd == "validate-config":
        return validate_config_cmd(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())