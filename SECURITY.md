# Security Policy

## Supported versions

Only the latest released version of `agentic-system` receives security fixes.
Pin the version (`pip install agentic-system==0.x.y`) and watch the
[releases page](https://github.com/Coding-Dev-Tools/agentic-system/releases)
for updates.

## Scope

`agentic-system` is an **optional orchestration layer** — it does not itself
execute arbitrary code, call out to the network, or run on untrusted input.
Security-relevant surface is intentionally small:

- It can refuse high-impact tool calls when a circuit breaker is OPEN
  (`breaker.high_impact_block_message`) — that gate must never silently
  allow. Bugs that let an OPEN breaker's high-impact actions through are
  security-relevant.
- It persists control-flow events and council verdicts to SQLite, and
  (optionally) to Engraphis. It does not redact secrets in payloads; hosts
  are responsible for not emitting secrets into the event stream.
- Optional integration installs third-party packages (`sentence-transformers`,
  `engraphis`) — supply-chain issues in those belong to their projects.

## Reporting a vulnerability

**Please do NOT open a public issue.** Email security concerns to the
maintainers via a private GitHub Security Advisory:

1. Go to <https://github.com/Coding-Dev-Tools/agentic-system/security/advisories/new>
2. Click "Report a vulnerability" and describe the issue + repro.

You'll get an acknowledgement within 5 business days. Please give us 90 days
to triage and ship a fix before any public disclosure. Coordinated disclosure
is happy to credit you in the release advisory.

## Hardening tips for hosts

- Run agent worker processes with least privilege; breakers gate destructive
  tools but cannot sandbox arbitrary subprocess execution.
- Set the events DB to a directory writable only by the agent process.
- Register a `CronPort` whose `scripts_dir` is not on a shared/tmp filesystem.