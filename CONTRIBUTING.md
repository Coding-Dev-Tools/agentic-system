# Contributing to agentic-system

Thanks for your interest in improving agentic-system! This is a small,
focused library, so contributions that keep it **framework-agnostic, dependency-
light, and well-tested** are the most welcome.

## Ground rules

- **The core depends only on the stdlib + pydantic (+ PyYAML for workflow
  YAML).** Don't add a hard dependency to the core; gate optional integration
  (Engraphis, sentence-transformers) behind an install extra.
- **No host-specific imports in the core.** Anything a host supplies (config,
  token budget, LLM client, cron) goes through one of the four ports in
  `agentic_system/ports.py` (`ConfigPort` / `TokenBudgetPort` / `LLMPort` /
  `CronPort`).
- **LLMs never control flow.** Only named FSM events / engine methods move
  state. Model output is data.

## Development

```bash
git clone https://github.com/Coding-Dev-Tools/agentic-system.git
cd agentic-system
python -m venv .venv && .venv/Scripts/activate   # or: source .venv/bin/activate
pip install -e ".[test]"
pytest                       # 114 tests, ~30s
ruff check agentic_system tests examples   # lint (syntax errors + pyflakes)
```

For the optional integrations:

```bash
pip install -e ".[test,embeddings]"     # semantic no-progress test runs
# engraphis tests use pytest.importorskip("engraphis"), so they skip unless
# the companion is installed.
```

## Before you open a PR

1. **Tests pass** — `pytest tests/ -q`. Add tests for any new behavior; we aim
   to keep coverage high (currently ~91%).
2. **Lint passes** — `ruff check agentic_system tests examples` is clean (the
   config is in `pyproject.toml`: `E9` + `F`).
3. **No build artifacts committed** — `dist/`, `*.whl`, `*.tar.gz` are
   gitignored; the CI `build` job asserts nothing leaks. Run `python -m build`
   locally if you change packaging.
4. **Public API changes** are reflected in the README and `CHANGELOG.md`.
5. **Keep docstrings self-contained** — no internal-design-doc citations or
   host-specific vocabulary in the core (`agent.orchestration_ports`,
   `hermes_events.db`, etc. belong to a host, not here).

## Commit + PR style

- Small, focused commits with a clear message (`feat:`, `fix:`, `docs:`,
  `test:`, `chore:`, `refactor:`).
- One logical change per PR. Rebase onto `main` before requesting review.

## Reporting issues

Open a [GitHub issue](https://github.com/Coding-Dev-Tools/agentic-system/issues)
with the agentic-system version (`python -c "import agentic_system; print(agentic_system.__version__)"`),
Python version, a minimal repro, and what you expected vs. got. For security
issues, see `SECURITY.md` (don't open a public issue).

## Releasing

1. Bump `version` in `pyproject.toml` + add a `CHANGELOG.md` entry.
2. Tag (`git tag v0.x.0 && git push --tags`).
3. `python -m build && python -m twine upload dist/*` (maintainers).
4. Publish a GitHub Release from the tag with the built artifacts attached.

## License

By contributing you agree your contributions are licensed MIT, same as the rest
of the project.