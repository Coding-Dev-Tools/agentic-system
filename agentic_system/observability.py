"""Observability: metrics (Prometheus), tracing (OTel), structured logging."""

from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable, Optional

# ── Structured logging ────────────────────────────────────────────────────

class StructuredFormatter(logging.Formatter):
    """JSON-like structured log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        base = {
            "ts": time.time(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Add extra fields
        for k, v in record.__dict__.items():
            if k not in {"name", "msg", "args", "created", "filename", "funcName",
                         "levelname", "levelno", "lineno", "module", "msecs",
                         "message", "name", "pathname", "process", "processName",
                         "relativeCreated", "thread", "threadName", "exc_info",
                         "exc_text", "stack_info"}:
                base[k] = v
        import json
        return json.dumps(base, default=str)


def setup_structured_logging(level: int = logging.INFO) -> None:
    """Replace root logger with structured JSON output."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


# ── Prometheus-style metrics (lightweight, no external dep) ───────────────

@dataclass
class Counter:
    name: str
    help: str = ""
    labels: tuple[str, ...] = ()
    _value: dict[tuple, int] = field(default_factory=dict)

    def inc(self, *label_values: str, amount: int = 1) -> None:
        key = label_values
        self._value[key] = self._value.get(key, 0) + amount

    def get(self, *label_values: str) -> int:
        return self._value.get(label_values, 0)


@dataclass
class Histogram:
    name: str
    help: str = ""
    labels: tuple[str, ...] = ()
    buckets: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
    _counts: dict[tuple, list[int]] = field(default_factory=dict)
    _sums: dict[tuple, float] = field(default_factory=dict)
    _total: dict[tuple, int] = field(default_factory=dict)

    def observe(self, value: float, *label_values: str) -> None:
        key = label_values
        if key not in self._counts:
            self._counts[key] = [0] * len(self.buckets)
            self._sums[key] = 0.0
            self._total[key] = 0
        for i, b in enumerate(self.buckets):
            if value <= b:
                self._counts[key][i] += 1
        self._sums[key] += value
        self._total[key] += 1

    def summary(self, *label_values: str) -> dict:
        key = label_values
        return {
            "count": self._total.get(key, 0),
            "sum": self._sums.get(key, 0.0),
            "buckets": dict(zip((str(b) for b in self.buckets), self._counts.get(key, []))),
        }


@dataclass
class Gauge:
    name: str
    help: str = ""
    labels: tuple[str, ...] = ()
    _value: dict[tuple, float] = field(default_factory=dict)

    def set(self, value: float, *label_values: str) -> None:
        self._value[label_values] = value

    def inc(self, amount: float = 1.0, *label_values: str) -> None:
        self._value[label_values] = self._value.get(label_values, 0.0) + amount

    def dec(self, amount: float = 1.0, *label_values: str) -> None:
        self.inc(-amount, *label_values)


# Global metric registry
_registry: dict[str, Counter | Histogram | Gauge] = {}


def counter(name: str, help: str = "", labels: tuple[str, ...] = ()) -> Counter:
    if name not in _registry:
        _registry[name] = Counter(name, help, labels)
    return _registry[name]


def histogram(name: str, help: str = "", labels: tuple[str, ...] = (),
              buckets: tuple[float, ...] = None) -> Histogram:
    if name not in _registry:
        _registry[name] = Histogram(name, help, labels, buckets or Histogram.buckets)
    return _registry[name]


def gauge(name: str, help: str = "", labels: tuple[str, ...] = ()) -> Gauge:
    if name not in _registry:
        _registry[name] = Gauge(name, help, labels)
    return _registry[name]


def export_metrics() -> str:
    """Export all metrics in Prometheus text format."""
    lines = []
    for m in _registry.values():
        lines.append(f"# HELP {m.name} {m.help}")
        lines.append(f"# TYPE {m.name} {type(m).__name__.lower()}")
        if isinstance(m, Counter):
            for labels, val in m._value.items():
                label_str = ",".join(f'{k}="{v}"' for k, v in zip(m.labels, labels))
                label_part = f"{{{label_str}}}" if label_str else ""
                lines.append(f"{m.name}{label_part} {val}")
        elif isinstance(m, Gauge):
            for labels, val in m._value.items():
                label_str = ",".join(f'{k}="{v}"' for k, v in zip(m.labels, labels))
                label_part = f"{{{label_str}}}" if label_str else ""
                lines.append(f"{m.name}{label_part} {val}")
        elif isinstance(m, Histogram):
            for labels, counts in m._counts.items():
                label_str = ",".join(f'{k}="{v}"' for k, v in zip(m.labels, labels))
                label_part = f"{{{label_str}}}" if label_str else ""
                for i, (b, c) in enumerate(zip(m.buckets, counts)):
                    le = "+Inf" if i == len(m.buckets) - 1 else str(m.buckets[i])
                    lines.append(f'{m.name}_bucket{{{label_part},le="{le}"}} {c}')
                lines.append(f'{m.name}_sum{{{label_part}}} {m._sums.get(labels, 0.0)}')
                lines.append(f'{m.name}_count{{{label_part}}} {m._total.get(labels, 0)}')
    return "\n".join(lines) + "\n"


# ── Built-in metrics ──────────────────────────────────────────────────────

COUNCIL_REVIEWS = counter("agentic_council_reviews_total", "Total council reviews",
                          ("gate", "decision", "cached"))
COUNCIL_LATENCY = histogram("agentic_council_latency_seconds", "Council review latency",
                            ("gate",))
BREAKER_STATE = gauge("agentic_breaker_state", "Breaker state (0=closed,1=half_open,2=open)",
                      ("level", "key"))
WORKFLOW_TASKS = counter("agentic_workflow_tasks_total", "Workflow tasks completed",
                         ("workflow", "task", "outcome"))
SWEEP_RUNS = counter("agentic_sweep_runs_total", "Sweep executions", ("sweep", "outcome"))


def record_council_review(gate: str, decision: str, latency: float, cached: bool = False) -> None:
    COUNCIL_REVIEWS.inc(gate, decision, "true" if cached else "false")
    COUNCIL_LATENCY.observe(latency, gate)


def record_breaker_state(level: str, key: str, state: str) -> None:
    val = {"CLOSED": 0, "HALF_OPEN": 1, "OPEN": 2}.get(state, 0)
    BREAKER_STATE.set(val, level, key)


def record_workflow_task(workflow: str, task: str, outcome: str) -> None:
    WORKFLOW_TASKS.inc(workflow, task, outcome)


def record_sweep(sweep: str, outcome: str) -> None:
    SWEEP_RUNS.inc(sweep, outcome)


# ── Context managers for timing ──────────────────────────────────────────

@contextmanager
def timed(name: str, labels: tuple[str, ...] = ()):
    """Context manager to time a block."""
    hist = histogram(f"agentic_{name}_seconds", f"Latency of {name}", labels)
    start = time.perf_counter()
    try:
        yield
    finally:
        hist.observe(time.perf_counter() - start, *labels)


def timed_async(name: str, labels: tuple[str, ...] = ()):
    """Async context manager for timing."""
    hist = histogram(f"agentic_{name}_seconds", f"Latency of {name}", labels)
    start = time.perf_counter()
    try:
        yield
    finally:
        hist.observe(time.perf_counter() - start, *labels)


# ── Decorators ────────────────────────────────────────────────────────────

def trace(name: Optional[str] = None, labels: tuple[str, ...] = ()):
    """Decorator to trace function calls with metrics."""
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        metric_name = name or fn.__name__
        hist = histogram(f"agentic_{metric_name}_seconds", f"Latency of {metric_name}", labels)
        counter = counter(f"agentic_{metric_name}_calls_total", f"Calls to {metric_name}", labels)

        @wraps(fn)
        def sync_wrapper(*args, **kwargs):
            c_key = tuple(str(a) for a in args[:len(labels)])
            counter.inc(*c_key)
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                hist.observe(time.perf_counter() - start, *c_key)

        @wraps(fn)
        async def async_wrapper(*args, **kwargs):
            c_key = tuple(str(a) for a in args[:len(labels)])
            counter.inc(*c_key)
            start = time.perf_counter()
            try:
                return await fn(*args, **kwargs)
            finally:
                hist.observe(time.perf_counter() - start, *c_key)

        import asyncio
        if asyncio.iscoroutinefunction(fn):
            return async_wrapper
        return sync_wrapper
    return decorator


# ── OpenTelemetry integration (optional) ──────────────────────────────────

_tracer = None


def init_otel(service_name: str = "agentic-system",
              endpoint: Optional[str] = None) -> bool:
    """Initialize OpenTelemetry tracing. Returns True if successful."""
    global _tracer
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME

        provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
        trace.set_tracer_provider(provider)

        if endpoint:
            exporter = OTLPSpanExporter(endpoint=endpoint)
        else:
            # Default to stdout for development
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter
            exporter = ConsoleSpanExporter()

        provider.add_span_processor(BatchSpanProcessor(exporter))
        _tracer = trace.get_tracer(__name__)
        return True
    except Exception:
        return False


def get_tracer():
    """Get the OTel tracer if initialized."""
    return _tracer


@contextmanager
def span(name: str, **attrs):
    """Create a span if OTel is initialized."""
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name, attributes=attrs) as sp:
        yield sp


# ── Health check endpoint data ────────────────────────────────────────────

@dataclass
class HealthCheck:
    name: str
    check: Callable[[], bool]
    critical: bool = True


async def run_health_checks(checks: list[HealthCheck]) -> dict[str, Any]:
    """Run all health checks, return status dict."""
    results = {}
    overall = "healthy"
    for c in checks:
        try:
            ok = c.check() if not asyncio.iscoroutinefunction(c.check) else await c.check()
            results[c.name] = "pass" if ok else "fail"
            if not ok and c.critical:
                overall = "unhealthy"
        except Exception as e:
            results[c.name] = f"error: {e}"
            if c.critical:
                overall = "unhealthy"
    return {"status": overall, "checks": results}


import asyncio