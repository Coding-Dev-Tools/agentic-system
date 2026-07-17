# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-07-17

### Added
- **8 Specialized Council Gates** extracted from Hermes battle-testing:
  - `code_edit` — mandatory review for ANY code edit
  - `pr_review` — two-pass review before creating/updating PR
  - `merge` — lenient review before merging (trusts PR gate)
  - `delegation` — lenient review before delegating coding task
  - `security` — vulnerability scan + gating (severity, exploitability, blast radius)
  - `code_quality` — complexity, duplication, test coverage, documentation, maintainability
  - `dependency` — vulnerability, license compliance, version freshness, supply chain
  - `architecture` — cohesion, coupling, scalability, observability, evolution
- **Gate-specific rubric dimensions** (5 dimensions each, tailored to gate purpose)
- **Circuit breaker self-heal**: global auto-recovers after cooldown; agent/workflow auto-close on task success
- **Provider failure tracking** in council: 15-min cooldown with weight redistribution to NVIDIA members
- **Security Council** module: gitleaks + semgrep scanning, PR gating, Engraphis verdict persistence
- **Workflow DAG engine**: CAS claiming, idempotent advance, restart-resume, background worker
- **Cron/Scheduler module**: SQLite-backed CronPort with APScheduler, persistent jobs across restarts
- **Periodic sweeps**: heartbeat (5m), stuck-task recovery (10m), metric watchdog (5m), nightly consolidate (3 AM)
- **Health CLI**: `python -m agentic_system.orchestration_status` — exits non-zero on OPEN breaker
- **EngraphisPort** in adapter seam for optional verdict persistence
- **No-progress detection**: optional semantic embeddings via `sentence-transformers` or custom callable

### Changed
- **CouncilService** now accepts `persist_hook` in constructor; `make_engraphis_persist_hook()` fixed to use `MemoryService.create()` with correct settings
- **BreakerRegistry** adds `try_self_heal()` and `record_success()` for auto-recovery
- **CronPort** interface adds `create_job(name, schedule, script, workdir)` signature
- **ConfigPort** adds `high_impact_tool_patterns()` for customizable tool gating

### Fixed
- **Engraphis persist hook** now correctly instantiates `MemoryService` (was calling nonexistent `create_default()`)
- **Council quorum logic**: constructor args now take precedence over config (consistent with members/thresholds)
- **Verdict cache** properly invalidates on council composition change

## [0.2.0] - 2026-07-09

### Added
- Initial framework-agnostic orchestration layer extracted from Hermes
- Three-level circuit breakers (agent/workflow/global) with high-impact tool gating
- Deterministic agent FSM with per-state tool policy
- Model Council (parallel reviews → weighted decision)
- No-progress detection (stdlib difflib)
- Workflow DAG engine (CAS claiming, idempotent advance)
- Periodic sweeps framework
- Read-only status/health CLI
- Engraphis verdict persistence hook

### Changed
- Removed all Hermes-specific code (de-Hermes-ified)
- Swappable adapter ports (ConfigPort, TokenBudgetPort, LLMPort, CronPort)

## [0.1.0] - 2026-07-08

### Added
- Initial release: core events, state machine, breakers, council, workflow, orchestration_status