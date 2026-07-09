# agentic-system

A framework-agnostic **orchestration layer for autonomous agents**. Drop it into
any Python agent runtime to get:

- **Durable event store** (SQLite WAL, append-only, replayable) — the system of record.
- **Deterministic agent FSM** with per-state tool policy — *LLMs never control flow.*
- **Three-level circuit breakers** (agent / workflow / global), persisted, with
  high-impact-tool gating (deploy/push/publish, including commands inside `terminal`).
- **No-progress loop detection** (stdlib difflib; pluggable embeddings upgrade).
- **Workflow DAG engine** (CAS claiming, idempotent advance, restart-resume) + worker.
- **Model Council** — parallel multi-model structured review → weighted verdict,
  peer-eval for high risk, verdict cache, optional Engraphis persistence.
- **Periodic sweeps** — heartbeat / stuck-task recovery / metric watchdog / nightly consolidate.
- **Read-only status / health CLI** — `python -m agentic_system.orchestration_status`
  (exits non-zero when any breaker is OPEN, so it doubles as a health check).

The core depends only on the **stdlib + pydantic**. Everything host-specific
(config, token budget, LLM, cron) is supplied through four **adapter ports** you
implement — so it works with Hermes, your own agent, or a custom runtime.

## Install

```bash
pip install agentic-system            # core: pydantic + PyYAML
pip install "agentic-system[engraphis]"   # optional: council verdict persistence
pip install "agentic-system[embeddings]"  # optional: semantic no-progress detection
```

## Wire it into your agent (the four ports)

```python
from agentic_system import ports

class MyConfig:                       # implement ports.ConfigPort
    def orchestration_enabled(self): return True
    def events_db_path(self): return "/var/lib/myagent/events.db"
    def council_config(self): return {                              # or None
        "members": [{"id": "my-model", "provider": "mine", "weight": 1.0}],
        "thresholds": {"min_overall": 4.0, "min_safety": 4.5,
                       "min_tests": 3.5, "min_agreement": 0.7, "reject_max_overall": 2.5},
        "peer_eval": "high_risk_only", "min_quorum": 2}
    def state_tool_policy(self): return None

class MyBudget:                       # implement ports.TokenBudgetPort
    def make(self, max_tokens): ...   # -> object with consume(tokens)/exceeded/used/max_total

def my_llm(member, system, user): ... # ports.LLMFn: (member, system, user) -> raw text

ports.set_config_port(MyConfig())
ports.set_token_budget_port(MyBudget())
ports.set_default_llm_fn(my_llm)      # used by the council when no llm_fn is passed
# ports.set_cron_port(MyCron())       # only needed if you use register_sweeps()
```

## Use it

```python
from agentic_system.events import hooks        # turn lifecycle / token-budget hooks
from agentic_system.state_machine import AgentStateMachine, filter_tools
from agentic_system.breakers import BreakerRegistry, high_impact_block_message
from agentic_system.council import CouncilService, CouncilRequest, make_engraphis_persist_hook
from agentic_system.workflow import WorkflowEngine, WorkflowWorker, load_directory
from agentic_system import sweeps

# emit turn events (no-op when orchestration_enabled() is False; never raises)
hooks.emit_turn_started(agent, task_id)
hooks.record_usage_ok(agent, total_tokens)
hooks.emit_turn_completed(agent, task_id, api_calls, total_tokens)

# gate high-impact tools when the global breaker is OPEN
if high_impact_block_message(tool_name, tool_args): ...  # refuse

# run a model council review
svc = CouncilService(db_path, persist_hook=make_engraphis_persist_hook())
decision = svc.review(CouncilRequest(subject_type="PR", content=diff, risk_level="medium"))

# run a workflow DAG to completion
engine = WorkflowEngine(db_path, definitions=load_directory())
worker = WorkflowWorker("agent-1", engine, handlers={"CODEGEN": my_codegen, ...})
while worker.run_once(): pass

# inspect the whole system
# $ python -m agentic_system.orchestration_status
```

## Status / health check

```bash
python -m agentic_system.orchestration_status          # human summary
python -m agentic_system.orchestration_status --json   # machine-readable
python -m agentic_system.orchestration_status --db /path/events.db --tail 50
# exit code 1 when any breaker is OPEN -> usable as a cron health check
```

## Sweeps

```python
from agentic_system.sweeps import heartbeat_sweep, stuck_task_sweep, metric_watchdog, daily_consolidate
heartbeat_sweep()          # stale agents -> UNRESPONSIVE, CAS their tasks back to PENDING
# or run from the CLI:
# $ python -m agentic_system.sweeps heartbeat
```

Register them as periodic jobs via `register_sweeps()` (needs a `CronPort`).

## Design invariants

- **LLMs never control flow** — only named FSM events / engine methods move state.
- **Events appended after state-table commits** (SQLite write-lock discipline).
- **Never delete** — event pruning archives JSONL first.
- **High-impact tools** (deploy/push/publish, incl. inside `terminal`) are refused
  while the global breaker is OPEN.
- **Graceful no-op** — with no ports registered the layer is inert and never raises.

## Status

Extracted from the Hermes agent's orchestration layer and made framework-agnostic.
Hermes is the reference consumer (its adapter implements the four ports against
`hermes_cli.config`, `auxiliary_client`, `cron.jobs`, `iteration_budget`).

License: MIT.