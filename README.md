# agentic-system

> A framework-agnostic orchestration layer for autonomous agents:
> circuit breakers, multi-LLM council (8 specialized gates), workflow FSM,
> periodic sweeps, and a read-only status/health CLI — all behind swappable
> host adapter ports so the layer drops into any Python agent runtime,
> not just Hermes.

**Version:** 0.3.0  
**License:** MIT  
**Python:** ≥3.11  
**Core deps:** `pydantic≥2.8`, `PyYAML≥6.0` (stdlib + pydantic only)

---

## What you get

| Capability | Description |
|------------|-------------|
| **Durable event store** | SQLite WAL, append-only, replayable — the system of record. |
| **Deterministic agent FSM** | Per-state tool policy — LLMs never control flow. |
| **Three-level circuit breakers** | Agent / workflow / global, persisted, with high-impact-tool gating (deploy/push/publish, including inside `terminal`). Self-heal: global auto-recovers after cooldown; agent/workflow auto-close on success. |
| **No-progress loop detection** | Stdlib `difflib` (catches verbatim loops). Optional semantic embeddings via `sentence-transformers` or custom callable (Engraphis cosine). |
| **Model Council** (8 gates) | Parallel structured reviews → weighted verdict (APPROVE/REWORK/REJECT). Verdict cache, quorum, peer-eval for high risk, Engraphis persistence. **Gates:** `code_edit`, `pr_review`, `merge`, `delegation`, `security`, `code_quality`, `dependency`, `architecture`. |
| **Workflow DAG engine** | CAS claiming, idempotent advance, restart-resume + background worker. |
| **Periodic sweeps** | Heartbeat / stuck-task recovery / metric watchdog / nightly consolidate — registered via CronPort. |
| **Status/health CLI** | `python -m agentic_system.orchestration_status` — exits non-zero when any breaker is OPEN (health-check ready). |

---

## Install

```bash
# From GitHub (PyPI publication pending)
pip install git+https://github.com/Coding-Dev-Tools/agentic-system.git

# Optional extras
pip install "agentic-system[engraphis]"      # council verdict persistence
pip install "agentic-system[embeddings]"     # semantic no-progress detection
```

---

## Quick Start

```python
# 1. Implement the four required ports for your runtime
from agentic_system import ports
from agentic_system.ports import ConfigPort, TokenBudgetPort, CronPort, LLMPort

class MyConfig(ConfigPort):
    def orchestration_enabled(self) -> bool: return True
    def events_db_path(self) -> str: return "/data/agent_events.db"
    def council_config(self) -> dict: return {
        "members": [
            {"id": "claude-sonnet-5", "provider": "anthropic", "weight": 1.1},
            {"id": "gpt-4o", "provider": "openai", "weight": 1.0},
        ],
        "thresholds": {"min_overall": 4.0, "min_safety": 4.5, "min_tests": 3.5,
                       "min_agreement": 0.7, "reject_max_overall": 2.5},
        "peer_eval": "high_risk_only",
        "min_quorum": 2,
    }
    def state_tool_policy(self) -> dict: return {...}  # optional override

class MyBudget(TokenBudgetPort):
    def make(self, max_tokens: int):
        return TokenBudget(max_tokens)  # your impl

def my_llm(member, system, user) -> str:
    return provider_call(member.id, system, user)  # your LLM router

ports.set_config_port(MyConfig())
ports.set_token_budget_port(MyBudget())
ports.set_default_llm_fn(my_llm)
ports.set_cron_port(MyCron())  # optional, for sweeps
```

```python
# 2. Use the orchestration layer
from agentic_system.breakers import high_impact_block_message
from agentic_system.no_progress import NoProgressDetector
from agentic_system.council import CouncilService, CouncilRequest

# Block high-impact tools when global breaker OPEN
if msg := high_impact_block_message("terminal", {"command": "git push origin main"}):
    raise PermissionError(msg)

# Detect no-progress loops
detector = NoProgressDetector(window=3, threshold=0.9)
if detector.record(agent_output):
    print("Loop detected!")

# Run a council review
council = CouncilService(db_path="/data/events.db")
decision = council.review(CouncilRequest(
    subject_type="CODE_EDIT",
    subject_ref={"repo": "myrepo", "file": "foo.py"},
    content=diff_text,
    risk_level="medium",
    gate="code_edit",
))
print(decision.decision, decision.metrics)
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  HOST RUNTIME (Hermes / your agent / custom)                │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │  Adapter seam (ports.py)                                │ │
│  │  ConfigPort • TokenBudgetPort • LLMPort • CronPort      │ │
│  │  EngraphisPort (optional)                               │ │
│  └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
┌───────────────┐  ┌───────────────┐  ┌───────────────┐
│   Events      │  │  State        │  │  Council      │
│   (SQLite     │  │  Machine      │  │  (8 gates)    │
│    WAL + bus) │  │  (per-state   │  │  cache/quorum │
└───────────────┘  │   tool policy)│  └───────────────┘
        ▲          └───────────────┘           ▲
        │                  ▲                   │
        │          ┌───────┴───────┐           │
        │          │  Workflow     │           │
        │          │  DAG Engine   │           │
        │          │  (CAS claim,  │           │
        │          │   idempotent) │           │
        │          └───────────────┘           │
        │                  ▲                   │
        └──────────────────┼───────────────────┘
                           ▼
              ┌───────────────────────┐
              │  Circuit Breakers     │
              │  (agent/wf/global)    │
              │  + high-impact gate   │
              └───────────────────────┘
                           ▲
                           │
              ┌───────────────────────┐
              │  Periodic Sweeps      │
              │  (heartbeat, stuck    │
              │   recovery, watchdog, │
              │   nightly consolidate)│
              └───────────────────────┘
```

---

## Core Modules

### `agentic_system.ports` — Adapter seam
Register your runtime's config, token budget, LLM router, cron, and optional Engraphis at startup.

```python
from agentic_system.ports import (
    set_config_port, set_token_budget_port,
    set_default_llm_fn, set_cron_port, set_engraphis_port,
)
```

### `agentic_system.events` — Durable event store + bus
- `EventBus` — in-process pub/sub with SQLite persistence
- `Event` — typed envelope (aggregate_id, correlation_id, priority)
- `connect()`, `ensure_state_tables()` — schema for breakers, council, workflow

### `agentic_system.state_machine` — Deterministic FSM
```python
from agentic_system.state_machine import AgentState, InvalidTransition
state = AgentState("PLANNING")
state.transition("EXECUTING", reason="plan approved")
state.can_use_tool("terminal")  # True/False per state policy
```

### `agentic_system.breakers` — Three-level circuit breakers
```python
from agentic_system.breakers import get_registry, high_impact_block_message

reg = get_registry()
reg.open("agent", "worker-42", "repeated failures")
reg.snapshot()  # list all breakers

# Tool gate (call before every high-impact tool)
if msg := high_impact_block_message(tool_name, tool_args):
    raise PermissionError(msg)
```

### `agentic_system.no_progress` — Loop detection
```python
from agentic_system.no_progress import NoProgressDetector, make_embedding_similarity

# Stdlib (verbatim/near-verbatim)
det = NoProgressDetector(window=3, threshold=0.9)

# Semantic (requires [embeddings] extra or custom callable)
det = NoProgressDetector(similarity=make_embedding_similarity())
```

### `agentic_system.council` — Model Council (8 gates)
```python
from agentic_system.council import CouncilService, CouncilRequest

council = CouncilService(
    db_path="/data/events.db",
    persist_hook=make_engraphis_persist_hook("hermes-council"),
)

decision = council.review(CouncilRequest(
    subject_type="CODE_EDIT",
    subject_ref={"repo": "acme", "file": "auth.py"},
    content=diff,
    risk_level="high",
    gate="code_edit",           # selects rubric dimensions
    checklist=["no secrets", "tests updated"],
))
# decision.decision ∈ {APPROVE, REWORK, REJECT}
```

**Gates & rubric dimensions:**
| Gate | Dimensions |
|------|------------|
| `code_edit` | correctness, safety, style, tests, minimal_change |
| `pr_review` | correctness, safety, tests, documentation, scope |
| `merge` | correctness, safety, tests, ci_status, branch_protection |
| `delegation` | feasibility, clarity, risk, dependencies, value |
| `security` | vuln_severity, exploitability, fix_correctness, blast_radius, compliance |
| `code_quality` | complexity, duplication, test_coverage, documentation, maintainability |
| `dependency` | vulnerability, license_compliance, version_freshness, supply_chain, breaking_changes |
| `architecture` | cohesion, coupling, scalability, observability, evolution |

### `agentic_system.workflow` — DAG engine
```python
from agentic_system.workflow import WorkflowEngine, TaskDef, WorkflowDef

engine = WorkflowEngine("/data/events.db")
engine.register_executor("lint", lambda inputs: {"report": run_linter(inputs["files"])})

wf = WorkflowDef("code_review", tasks=(
    TaskDef("lint", outputs=("lint_report",)),
    TaskDef("type_check", outputs=("type_report",)),
    TaskDef("test", inputs=("lint_report", "type_report"), outputs=("test_report",)),
))
inst = engine.create_instance(wf, {"files": ["src/"]})
```

Background worker:
```bash
python -m agentic_system.workflow.worker worker-0
```

### `agentic_system.sweeps` — Periodic sweeps
```python
from agentic_system.sweeps import register_sweeps
register_sweeps()  # writes scripts to CronPort.scripts_dir(), registers jobs
```
Built-in sweeps:
- `heartbeat` (5m) — agent liveness + work-log
- `stuck_task_recovery` (10m) — re-queue stalled workflow tasks
- `metric_watchdog` (5m) — breaker OPEN, queue depth, error rate → ALERT.md
- `nightly_consolidate` (3 AM) — archive old events, vacuum DB

### `agentic_system.orchestration_status` — Health CLI
```bash
python -m agentic_system.orchestration_status
# JSON for monitoring:
python -m agentic_system.orchestration_status --json
```
Exit codes: `0=healthy`, `1=breaker OPEN`, `2=sweeps overdue`, `3=bus error`, `4=disabled`.

---

## Configuration (host-supplied via ConfigPort)

```yaml
council:
  members:
    - id: "claude-sonnet-5"
      provider: "anthropic"
      weight: 1.1
    - id: "gpt-4o"
      provider: "openai"
      weight: 1.0
  thresholds:
    min_overall: 4.0
    min_safety: 4.5
    min_tests: 3.5
    min_agreement: 0.7
    reject_max_overall: 2.5
  peer_eval: "high_risk_only"   # never | high_risk_only | always
  min_quorum: 2

# Optional: self-heal tuning
breakers:
  global_auto_recover_seconds: 300
  agent_auto_close_on_success: true
  workflow_auto_close_on_success: true

# Optional: no-progress
no_progress:
  window: 3
  threshold: 0.9
  # embedding backend auto-detected; or set explicitly:
  # embedding_backend: "sentence-transformers"
```

---

## Engraphis Integration (council verdict persistence)

```python
from agentic_system.council import make_engraphis_persist_hook
from agentic_system.council import CouncilService

council = CouncilService(
    db_path="/data/events.db",
    persist_hook=make_engraphis_persist_hook("hermes-council"),
)
```
Verdicts land in Engraphis namespace `hermes-council` as `kind=council_verdict` episodic memories — available for `engraphis_why`, `engraphis_timeline`, `engraphis_recall_grounded`.

---

## Development

```bash
git clone https://github.com/Coding-Dev-Tools/agentic-system.git
cd agentic-system
pip install -e ".[dev]"
ruff check .
mypy agentic_system
pytest -v
```

---

## Extracted from Hermes

This package is the **framework-agnostic core** extracted from the [Hermes agent](https://github.com/Coding-Dev-Tools/hermes-agent). Hermes implements the five ports against its config system (`hermes_cli.config`), token budget (`iteration_budget.TokenBudget`), cron (`cron.jobs`), auxiliary LLM client, and Engraphis MCP server.

---

## License

MIT — see [LICENSE](LICENSE).