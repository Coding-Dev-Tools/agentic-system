#!/usr/bin/env python3
"""Example: Observability — Prometheus metrics, OpenTelemetry tracing, structured logging."""

import time
import asyncio
from agentic_system.observability import (
    setup_structured_logging, StructuredFormatter,
    counter, histogram, gauge, export_metrics,
    record_council_review, record_breaker_state, record_workflow_task, record_sweep,
    timed, timed_async, trace, init_otel, get_tracer, span,
)

# ── 1. Structured JSON Logging ────────────────────────────────────────────

def demo_structured_logging():
    print("=== 1. Structured JSON Logging ===")
    setup_structured_logging()
    logger = logging.getLogger("demo")

    # Regular log with extra fields
    logger.info("Agent started", extra={
        "agent_id": "worker-42",
        "task_id": "task-123",
        "model": "claude-sonnet-5",
        "tokens_used": 1542,
    })

    # Error with context
    try:
        raise ValueError("API rate limit exceeded")
    except Exception as e:
        logger.error("LLM call failed", extra={
            "error_type": type(e).__name__,
            "provider": "anthropic",
            "retry_count": 2,
        }, exc_info=True)

    print("  Check stdout for JSON log lines\n")


# ── 2. Prometheus-style Metrics ────────────────────────────────────────────

def demo_metrics():
    print("=== 2. Prometheus Metrics ===")

    # Counters
    requests = counter("agent_requests_total", "Total agent requests", ("agent", "outcome"))
    requests.inc("worker-1", "success")
    requests.inc("worker-1", "success")
    requests.inc("worker-2", "error")

    # Histograms
    latency = histogram("agent_latency_seconds", "Agent response latency", ("agent",))
    for _ in range(10):
        latency.observe(time.random() * 2, "worker-1")

    # Gauges
    active = gauge("agent_active_count", "Active agents")
    active.set(3, "worker-1")
    active.set(5, "worker-2")

    # Built-in metrics (council, breakers, workflows, sweeps)
    record_council_review("code_edit", "APPROVE", 1.2, cached=False)
    record_breaker_state("global", "system", "OPEN")
    record_workflow_task("code_review", "lint", "success")
    record_sweep("heartbeat", "ok")

    # Export Prometheus format
    print("  Prometheus metrics output:")
    print(export_metrics()[:500] + "...")


# ── 3. Timing Decorators ──────────────────────────────────────────────────

def demo_timing():
    print("\n=== 3. Timing Decorators ===")

    @trace("llm_call", labels=("provider",))
    def call_llm(provider: str, prompt: str) -> str:
        time.sleep(0.01)
        return f"response from {provider}"

    @timed("db_query", labels=("query_type",))
    def query_db(query_type: str) -> list:
        time.sleep(0.005)
        return [1, 2, 3]

    # Sync calls
    call_llm("anthropic", "Hello")
    query_db("select")

    # View metrics
    print("  Metrics:")
    print(f"  agentic_llm_call_seconds bucket: agentic_llm_call_seconds_bucket...")
    print(f"  agentic_db_query_seconds bucket: agentic_db_query_seconds_bucket...")


# ── 4. Async Support ──────────────────────────────────────────────────────

async def demo_async():
    print("\n=== 4. Async Timing ===")

    @timed_async("async_api_call", labels=("endpoint",))
    async def fetch_data(endpoint: str) -> dict:
        await asyncio.sleep(0.01)
        return {"data": "ok"}

    @trace("async_workflow", labels=("stage",))
    async def process():
        await fetch_data("/api/users")
        await fetch_data("/api/posts")

    await process()


# ── 5. OpenTelemetry Tracing ──────────────────────────────────────────────

def demo_otel():
    print("\n=== 5. OpenTelemetry Tracing ===")

    # Initialize (requires opentelemetry-sdk, opentelemetry-exporter-otlp)
    success = init_otel("agentic-system-demo")
    if not success:
        print("  OTel not available (install opentelemetry-sdk, opentelemetry-exporter-otlp)")
        return

    tracer = get_tracer()

    with tracer.start_as_current_span("agent_turn") as sp:
        sp.set_attribute("agent.id", "worker-1")
        sp.set_attribute("model", "claude-sonnet-5")

        with tracer.start_as_current_span("llm_call") as child:
            child.set_attribute("provider", "anthropic")
            child.set_attribute("tokens.input", 1200)
            child.set_attribute("tokens.output", 300)
            time.sleep(0.01)

        with tracer.start_as_current_span("tool_call") as child:
            child.set_attribute("tool", "terminal")
            child.set_attribute("duration_ms", 45)

    print("  Spans exported to OTLP endpoint (or stdout if no endpoint)")


# ── 6. Health Checks ──────────────────────────────────────────────────────

async def demo_health():
    print("\n=== 6. Health Checks ===")
    from agentic_system.observability import HealthCheck, run_health_checks

    checks = [
        HealthCheck("database", lambda: True, critical=True),
        HealthCheck("llm_api", lambda: True, critical=True),
        HealthCheck("cache", lambda: False, critical=False),  # non-critical failure
    ]

    result = await run_health_checks(checks)
    print(f"  Overall: {result['status']}")
    for name, status in result['checks'].items():
        print(f"  {name}: {status}")


def main():
    demo_structured_logging()
    demo_metrics()
    demo_timing()
    asyncio.run(demo_async())
    demo_otel()
    asyncio.run(demo_health())


if __name__ == "__main__":
    main()