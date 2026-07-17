# Contributing to agentic-system

Thank you for your interest in contributing! This project follows a standard GitHub workflow.

## Development Setup

```bash
# Clone and install in development mode
git clone https://github.com/Coding-Dev-Tools/agentic-system.git
cd agentic-system
pip install -e ".[dev]"
```

## Code Quality

We enforce strict quality standards:

```bash
# Format
ruff format agentic_system tests

# Lint
ruff check agentic_system tests

# Type check
mypy agentic_system

# Tests
pytest -v --cov=agentic_system --cov-fail-under=80
```

All CI checks must pass before merge.

## Architecture Principles

1. **Framework-agnostic**: Core depends only on stdlib + pydantic. Host-specific code lives in adapter ports.
2. **No hidden control flow**: LLMs never move state — only FSM events / engine methods do.
3. **Durability first**: Events are append-only, never deleted. Pruning archives to JSONL first.
4. **Graceful no-op**: With no ports registered, the layer is inert and never raises.
5. **Observability built-in**: Structured logging, Prometheus metrics, OTel tracing, health checks.

## Adding a New Council Gate

1. Add gate name to `GATE_DIMENSIONS` in `council/schemas.py`
2. Add gate-specific system prompt in `council/service.py` (`GATE_SYSTEMS`)
3. Add test in `tests/test_council.py`
4. Document in `README.md` gates table

## Adding a New Sweep

1. Add sweep definition to `SCRIPTS` in `sweeps.py`
2. Write script with `main()` that exits 0 on success, non-zero on failure
3. Add test in `tests/test_register_sweeps.py`
4. Document in `README.md`

## Adding a New Port Method

1. Define new `Protocol` in `ports.py`
2. Add getter/setter/reset in `ports.py`
3. Update `reset_ports_for_tests()`
4. Document in `README.md` and `MIGRATION_GUIDE.md`

## Testing Guidelines

- Unit tests for pure logic (state machine, breakers, no-progress)
- Integration tests for DB-backed components (council, workflow, events)
- Mock host ports using `FakeConfigPort`, `FakeTokenBudgetPort`, etc.
- Test both sync and async paths where applicable

## Release Process

1. Update `CHANGELOG.md` with new version
2. Bump version in `pyproject.toml` and `agentic_system/__init__.py`
3. Tag: `git tag vX.Y.Z && git push origin vX.Y.Z`
4. GitHub Actions builds and publishes to PyPI

## Code of Conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Be respectful, inclusive, and constructive.

## Security

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.