# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.3.x   | :white_check_mark: |
| 0.2.x   | :x:                |
| 0.1.x   | :x:                |

## Reporting a Vulnerability

Please report security vulnerabilities privately:

1. Email: **security@coding-dev-tools.org**
2. Or use GitHub's private vulnerability reporting (Security tab → "Report a vulnerability")

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

We will acknowledge receipt within 48 hours and provide a timeline for fix.

## Security Considerations

### Agentic-System Specific

1. **Circuit Breakers**: The global breaker blocks high-impact tools (deploy/push/publish). Misconfiguration could block legitimate operations. Test in staging first.

2. **Model Council**: Council decisions affect code merges/deployments. Ensure:
   - Council members are from trusted providers
   - Thresholds are appropriate for your risk tolerance
   - Verdict cache doesn't stale-approve changed artifacts

3. **Workflow Engine**: CAS claiming prevents double-execution. Ensure idempotency keys are unique per task+inputs.

4. **Event Store**: Events are append-only with WAL. Ensure DB path is on durable storage.

5. **Sweeps**: Nightly consolidate archives old events. Verify `EVENT_RETENTION_DAYS` meets compliance.

### General

- Keep dependencies updated (`pip-audit` / `dependabot`)
- Use virtual environments
- Never commit secrets (API keys, tokens) to version control
- Run `pip install -e ".[dev]"` and `ruff check` before committing

## Disclosure Policy

We follow responsible disclosure. Once a fix is ready, we will:
1. Release a patch version
2. Publish a security advisory on GitHub
3. Credit the reporter (unless anonymity requested)

## Contact

Security team: **security@coding-dev-tools.org**