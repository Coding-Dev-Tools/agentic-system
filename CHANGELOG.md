# Changelog

All notable changes to agentic-system are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-07-11

Public-launch polish: self-contained for new users, framework-agnostic, type-checked.

### Added
- **breakers** — `breaker_recovery_sweep()` (OPEN→HALF_OPEN after cooldown,
  HALF_OPEN→CLEAN→CLOSED or re-OPEN). Registered as 5th sweep (`*/2 * * * *`).
- **no_progress** — `NoProgress` exception + `NoProgressDetector.raise_if_looping()`
  convenience; worker maps raised `NoProgress` to FSM `no_progress` event.
- **state_machine** — explicit `no_progress` FSM event (EXECUTING→FAILED).
- **council** — constructor args now win over config for ALL fields (members,
  thresholds, peer_eval, min_quorum).
- **mypy CI job** — guards the `py.typed` contract (0 errors, 21 source files).
- **governance** — `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`,
  issue/PR templates, Dependabot config.

### Changed
- All `handoff §X.Y` internal-design-doc citations removed from docstrings and
  comments — the code is now self-documenting for adopters with no internal context.
- `pm2` references replaced with `worker processes`; `hermes_events.db` replaced
  with `events DB`; em-dashes normalized to `--` throughout.
- Docstrings cleaned of host-specific vocabulary (`hermes_cli.config`,
  `cli-config.yaml`, `_cowork_ops`, `agent.orchestration_ports`).
- `orchestration_status` helper `_count()` replaces an inline lambda for
  clearer type annotations.

### Fixed
- `EventStore.append_many` now uses a single transaction (one commit, not one per
  event).
- `EventStore.append` / `append_many` guard `lastrowid is not None` before `int()`.
- Workflow `statuses` dict typed as `dict[str, str]` (was inferred as
  `dict[str, str | None]`).
- `make_engraphis_persist_hook`: `out.get("id")` return annotated as
  `Optional[str]` (was `Any`).
- Type annotations across the package tightened: `_extract_json` returns
  `dict[str, Any]`, sweep schedule list args typed as `list[tuple[str, str]]`.

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