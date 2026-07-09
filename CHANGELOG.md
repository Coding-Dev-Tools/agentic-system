# Changelog

All notable changes to agentic-system are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-07-09

Initial public release. Extracted from the Hermes agent's orchestration layer and
made framework-agnostic via four swappable adapter ports.

### Added
- **events** — append-only SQLite WAL event store, outbox bus, pydantic-validated
  envelope, flag-gated hooks (turn lifecycle + token budget), shared state tables.
- **state_machine** — deterministic agent FSM + per-state tool policy
  (`LLMs never control flow`).
- **breakers** — three-level circuit breakers (agent / workflow / global), persisted,
  with high-impact tool/command gating (deploy/push/publish, incl. inside `terminal`).
- **no_progress** — sliding-window loop detector (stdlib difflib; embeddings upgrade path).
- **council** — parallel multi-model structured review → weighted verdict, peer-eval
  for high risk, verdict cache, optional Engraphis persistence (`make_engraphis_persist_hook`).
- **workflow** — DAG engine (CAS claims, idempotent advance, restart-resume), worker,
  shipped `refactor_sweep` + `review_and_test` definitions.
- **sweeps** — heartbeat / stuck-task / metric-watchdog / nightly-consolidate,
  `CronPort`-based registration.
- **orchestration_status** — read-only status / health CLI (exit 1 on OPEN breaker).
- **ports** — the host adapter seam: `ConfigPort`, `TokenBudgetPort`, `LLMPort`,
  `CronPort` (Protocol interfaces + swappable registry).
- Optional extras: `[engraphis]` (council verdict persistence), `[embeddings]`
  (semantic no-progress detection), `[test]`.

### Design invariants
- LLMs never control flow.
- Events appended after state-table commits.
- Never delete — pruning archives first.
- Graceful no-op with no ports registered (never raises).