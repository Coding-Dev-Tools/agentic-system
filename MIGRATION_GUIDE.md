# Migration Guide: v0.2 → v0.3

## Breaking Changes

### 1. Council Gates Added (New Feature, Not Breaking)
The council now supports 8 specialized gates. Existing code using generic `CouncilRequest` continues to work but can now specify a `gate` parameter for tailored rubrics.

**Before (v0.2):**
```python
decision = council.review(CouncilRequest(
    subject_type="PR",
    content=diff,
    risk_level="high",
))
```

**After (v0.3) - Optional gate specification:**
```python
decision = council.review(CouncilRequest(
    subject_type="PR",
    content=diff,
    risk_level="high",
    gate="pr_review",  # NEW: uses gate-specific dimensions
    checklist=["tests updated", "docs updated"],
))
```

### 2. ConfigPort Extended
Added `high_impact_tool_patterns()` method to ConfigPort. Host implementations should add this method:

```python
class MyConfig(ConfigPort):
    # ... existing methods ...

    def high_impact_tool_patterns(self) -> Optional[tuple[str, ...]]:
        return ("deploy*", "*git_push*", "*push_to_remote*",
                "publish*", "release*", "*prod*deploy*")
```

### 3. EngraphisPort Added (Optional)
New optional port for council verdict persistence. If not implemented, council works without Engraphis.

```python
class MyEngraphisPort:
    def remember(self, content: str, workspace: str, mtype: str = "episodic",
                 scope: str = "workspace", title: str = "",
                 source: str = "agent:council", kind: str = "council_verdict",
                 importance: float = 0.6) -> Optional[str]:
        # Your Engraphis integration
        return "mem_123"
```

Register at startup:
```python
from agentic_system.ports import set_engraphis_port
set_engraphis_port(MyEngraphisPort())
```

### 4. Breaker Self-Heal (Behavior Change)
Global breaker now auto-recovers to HALF_OPEN after `global_auto_recover_seconds` (default 300s). To disable, set to 0 or a very large number in config.

### 5. Provider Failure Tracking (New)
Council now tracks provider failures with 15-min cooldown and weight redistribution to NVIDIA members. No code changes needed; behavior is automatic.

---

## New Features (Opt-In)

### 1. Async Event Bus
```python
from agentic_system.events.async_bus import AsyncEventBus, review_async

# Async council review
decision = await review_async(request, db_path="/data/events.db")
```

### 2. CLI Commands
```bash
agentic-system status              # Health check (exit 1 if global OPEN)
agentic-system breakers --list     # List all breakers
agentic-system council --review    # Run a review
agentic-system workflow --list     # List workflows
agentic-system sweeps --register   # Register cron sweeps
agentic-system init ./my-project   # Scaffold new project
```

### 3. Observability
```python
from agentic_system.observability import (
    setup_structured_logging,
    timed, trace, counter, histogram,
    init_otel, export_metrics,
)

setup_structured_logging()
init_otel("my-agent", "http://otel-collector:4317")

@trace("my_operation", ("type",))
def my_op():
    with timed("my_operation"):
        do_work()

# Prometheus metrics
print(export_metrics())
```

### 4. Security Council Module
```python
from agentic_system.security_council import SecurityCouncil, SecurityCouncilConfig

sec = SecurityCouncil(
    db_path="/data/events.db",
    config=SecurityCouncilConfig(
        scan_interval_minutes=60,
        repos=["/path/to/repo1", "/path/to/repo2"],
        severity_threshold="medium",
    ),
)
sec.start()  # Background scanner + PR gating
```

---

## Configuration Updates

### v0.2 Config (still works)
```yaml
council:
  members:
    - id: "claude-sonnet-5"
      provider: "anthropic"
      weight: 1.1
  thresholds:
    min_overall: 4.0
  peer_eval: "high_risk_only"
  min_quorum: 2
```

### v0.3 Config (recommended additions)
```yaml
council:
  members:
    - id: "claude-sonnet-5"
      provider: "anthropic"
      weight: 1.1
    - id: "gpt-4o"
      provider: "openai"
      weight: 1.0
    - id: "nemotron-3-ultra"
      provider: "nvidia"
      weight: 1.2  # NVIDIA members get weight redistribution during cooldown
  thresholds:
    min_overall: 4.0
    min_safety: 4.5
    min_tests: 3.5
    min_agreement: 0.7
    reject_max_overall: 2.5
  peer_eval: "high_risk_only"
  min_quorum: 2

breakers:
  global_auto_recover_seconds: 300
  agent_auto_close_on_success: true
  workflow_auto_close_on_success: true

no_progress:
  window: 3
  threshold: 0.9
  embedding_backend: "auto"

sweeps:
  heartbeat_enabled: true
  heartbeat_schedule: "*/5 * * * *"
  stuck_recovery_enabled: true
  stuck_recovery_schedule: "*/10 * * * *"
  stuck_threshold_min: 30
  watchdog_enabled: true
  watchdog_schedule: "*/5 * * * *"
  consolidate_enabled: true
  consolidate_schedule: "0 3 * * *"
  event_retention_days: 30
```

---

## Testing Migration

Run the test suite to verify compatibility:
```bash
pip install -e ".[dev]"
pytest -v
```

All v0.2 tests should pass. New tests cover gates, self-heal, provider tracking.

---

## Need Help?

- Check [CHANGELOG.md](CHANGELOG.md) for detailed changes
- Open an issue on GitHub for migration questions
- See `examples/` for updated usage patterns