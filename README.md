# agentic-system

A framework-agnostic **orchestration layer for autonomous agents**. Drop it into
any Python agent runtime to get:

- **Durable event store** (SQLite WAL, append-only, replayable) — the system of record.
- **Deterministic agent FSM** with per-state tool policy — *LLMs never control flow.*
- **Three-level circuit breakers** (agent / workflow / global), persisted, with
  high-impact-tool gating (deploy/push/publish, including commands inside `terminal`).
- **No-progress loop detection** (stdlib difflib; pluggable embeddings upgrade).
- **Workflow DAG engine** (CAS claiming, idempotent advance, restart-resume) + worker.
- **Model Council** — deadline-bounded multi-model review, deterministic named
  or custom gate policies, peer evaluation, safe caching, and optional Engraphis persistence.
- **Periodic sweeps** — heartbeat / stuck-task recovery / metric watchdog / nightly consolidate.
- **Read-only status / health CLI** — `python -m agentic_system.orchestration_status`
  (exits non-zero when any breaker is OPEN, so it doubles as a health check).

The core depends only on the **stdlib + pydantic**. Everything host-specific
(config, token budget, LLM, cron) is supplied through four **adapter ports** you
implement — so it works with Hermes, your own agent, or a custom runtime.

## Install

**From GitHub** (available now — PyPI publication is the final pending step):

```bash
pip install agentic-system            # core: pydantic + PyYAML
# pip install "agentic-system[engraphis]"   # optional: council verdict persistence (once Engraphis is on PyPI)
# pip install "agentic-system[embeddings]"  # optional: semantic no-progress detection
```

Until the package is on PyPI, install the wheel/sdist from the [releases page](https://github.com/Coding-Dev-Tools/agentic-system/releases), or from source:

```bash
pip install git+https://github.com/Coding-Dev-Tools/agentic-system.git
# extras:
pip install "agentic-system[embeddings] @ git+https://github.com/Coding-Dev-Tools/agentic-system.git"
# council verdict persistence (install the companion from its GitHub first):
pip install git+https://github.com/Coding-Dev-Tools/engraphis.git
```

## Wire it into your agent (the four ports)

```python
from agentic_system import ports

class MyConfig:                       # implement ports.ConfigPort
    def orchestration_enabled(self): return True
    def events_db_path(self): return "/var/lib/myagent/events.db"
    def council_config(self): return {                              # or None
        "members": [
            {"id": "reviewer-a", "provider": "mine", "weight": 1.0},
            {"id": "reviewer-b", "provider": "mine", "weight": 1.0},
        ],
        "thresholds": {"min_overall": 4.0, "min_safety": 4.5,
                       "min_tests": 3.5, "min_agreement": 0.7,
                       "reject_max_overall": 2.5},
        "peer_eval": "high_risk_only", "min_quorum": 2,
        "review_timeout_seconds": 60, "max_parallel_reviews": 8,
        "max_content_chars": 100_000, "max_model_response_chars": 50_000,
    }
    def state_tool_policy(self): return None

class MyBudget:                       # implement ports.TokenBudgetPort
    def make(self, max_tokens): ...   # -> object with consume(tokens)/exceeded/used/max_total

def my_llm(member, system, user, *, timeout_seconds):
    ...  # pass timeout_seconds to the provider's HTTP/API timeout

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

# run a deadline-bounded PR review under a deterministic gate policy
svc = CouncilService(
    db_path, persist_hook=make_engraphis_persist_hook(),
    review_timeout_seconds=60,
)
decision = svc.review(CouncilRequest(
    subject_type="PR", content=diff, risk_level="medium", gate="pr_review",
))

# run a workflow DAG to completion
engine = WorkflowEngine(db_path, definitions=load_directory())
worker = WorkflowWorker("agent-1", engine, handlers={"CODEGEN": my_codegen, ...})
while worker.run_once(): pass

# inspect the whole system
# $ python -m agentic_system.orchestration_status
```

## Council gates and deadlines

Built-in gates are `code_edit`, `pr_review`, `merge`, `delegation`, `security`,
`code_quality`, `dependency`, and `architecture`. Each declares its exact
dimensions, whether higher or lower scores are better, and approval floors.
`GatePolicy` and `DimensionPolicy` provide the same contract for host-specific
gates; policy checks run in Python after model output validation.

Tool-backed facts belong in `evidence_scores`. They override every model's score
for that dimension. The `merge` gate requires host-derived `ci_status` and
`branch_protection` scores, so model claims cannot turn failed CI into approval:

```python
request = CouncilRequest(
    subject_type="MERGE",
    subject_ref={"repo": "org/project", "pr": 42},
    content=diff,
    gate="merge",
    evidence={"ci_url": ci_url, "required_checks": required_checks},
    evidence_scores={"ci_status": 5, "branch_protection": 5},
)
decision = svc.review(request)
```

`review_timeout_seconds` is one shared deadline across review and peer-evaluation
calls. Adapters accepting the keyword-only `timeout_seconds` argument can cancel
provider I/O cooperatively. Legacy three-argument adapters remain compatible;
their late calls are discarded and cannot delay the returned verdict, but may
finish in a background thread. Per-member outcomes distinguish `success`,
`timeout`, `provider_error`, `invalid_output`, and `cooldown`. Only complete
sessions are cached; cache identity includes policy, risk, thresholds, model
providers and weights, while `cache_ttl_seconds=0` disables replay.
Cache hits set `cached=True` while preserving the original decision reason.

## Status / health check

```bash
python -m agentic_system.orchestration_status          # human summary
python -m agentic_system.orchestration_status --json   # machine-readable
python -m agentic_system.orchestration_status --db /path/events.db --tail 50
# exit code 1 when any breaker is OPEN -> usable as a cron health check
```

## Sweeps

```python
from agentic_system.sweeps import (
    heartbeat_sweep, stuck_task_sweep, metric_watchdog,
    breaker_recovery_sweep, daily_consolidate)
heartbeat_sweep()          # stale agents -> UNRESPONSIVE, CAS their tasks back to PENDING
daily_consolidate()        # archive-then-prune old events to _archive/
# or run from the CLI:
# $ python -m agentic_system.sweeps heartbeat
```

The `metric_watchdog` **trips** circuit breakers from failure metrics;
`breaker_recovery_sweep` **self-heals** them (OPEN -> HALF_OPEN after a cooldown,
then HALF_OPEN -> CLOSED on a clean probe, or re-OPEN if failures continue) —
without it, a tripped breaker stays OPEN until a manual `close()`.

Register all of them as periodic jobs via `register_sweeps()` (needs a `CronPort`).

## Using with Engraphis (the companion memory engine)

[Engraphis](https://github.com/Coding-Dev-Tools/engraphis) is a local-first AI memory engine. agentic-system
works hand-in-hand with it via a single hook — council verdicts persist to
Engraphis as durable `council_verdict` memories (episodic, workspace-scoped) so
they show up in recall/why/timeline alongside everything else your agent knows.

```bash
pip install git+https://github.com/Coding-Dev-Tools/agentic-system.git   # the orchestration layer
pip install git+https://github.com/Coding-Dev-Tools/engraphis.git          # the memory engine (base install is enough)
# (replace with `pip install agentic-system` / `pip install engraphis` once both are on PyPI)
```

```python
from agentic_system.council import CouncilService, CouncilRequest, make_engraphis_persist_hook

svc = CouncilService(db_path, persist_hook=make_engraphis_persist_hook())
decision = svc.review(CouncilRequest(subject_type="PR", content=diff, risk_level="medium"))
# decision is now also a durable Engraphis memory you can recall/why/timeline.
```

Zero-config: `make_engraphis_persist_hook()` writes to Engraphis's default DB
(`ENGRAPHIS_DB_PATH`), auto-creates the `hermes-council` workspace, and **never
crashes the council** — if Engraphis isn't installed or can't be built, the hook
becomes a no-op and logs a warning. Real semantic recall needs `engraphis[mcp]`;
the base install uses a deterministic embedder fallback (still durable, just not
semantic-search-ranked).

### Optional: semantic no-progress detection

The default `NoProgressDetector` uses difflib (catches verbatim loops). For
semantic looping, back it with embeddings — either sentence-transformers or
your own callable against Engraphis's embedder:

```bash
pip install "agentic-system[embeddings]"   # adds sentence-transformers
```
```python
from agentic_system.no_progress import NoProgressDetector
from agentic_system.embedding_similarity import make_embedding_similarity
det = NoProgressDetector(window=3, threshold=0.9, similarity=make_embedding_similarity())
# or build your own:  det = NoProgressDetector(similarity=my_engraphis_cosine)
```

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