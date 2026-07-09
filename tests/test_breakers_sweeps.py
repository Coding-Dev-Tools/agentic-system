"""Phase 2 tests: breaker registry + the four deterministic sweeps."""

from datetime import datetime, timedelta, timezone

import pytest

from agentic_system.breakers import BreakerRegistry, GLOBAL_KEY
from agentic_system.events.bus import EventBus
from agentic_system.events.envelope import EventEnvelope
from agentic_system.events.state_tables import connect, ensure_state_tables, heartbeat
from agentic_system.events.store import EventStore
from agentic_system.sweeps import (
    daily_consolidate,
    heartbeat_sweep,
    metric_watchdog,
    stuck_task_sweep,
)


def _iso_ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "events.db")
    conn = connect(p)
    ensure_state_tables(conn)
    yield p, conn
    conn.close()


# ── breakers ──────────────────────────────────────────────────────────────

def test_breaker_lifecycle_and_persistence(db, tmp_path):
    p, _ = db
    alert = tmp_path / "ALERT.md"
    r1 = BreakerRegistry(p, alert_path=str(alert))
    r1.open("workflow", "refactor_sweep", "fail ratio")
    r1.close_conn()
    r2 = BreakerRegistry(p, alert_path=str(alert))  # restart survives
    assert r2.is_open("workflow", "refactor_sweep")
    assert not r2.should_accept_task(workflow="refactor_sweep")
    assert r2.should_accept_task(workflow="other")
    r2.half_open("workflow", "refactor_sweep")
    assert not r2.is_open("workflow", "refactor_sweep")  # HALF_OPEN allows probe
    r2.close("workflow", "refactor_sweep")
    assert r2.state("workflow", "refactor_sweep") == "CLOSED"
    r2.close_conn()


def test_global_open_blocks_everything_and_writes_alert(db, tmp_path):
    p, _ = db
    alert = tmp_path / "ALERT.md"
    r = BreakerRegistry(p, alert_path=str(alert))
    r.open("global", GLOBAL_KEY, "cost spike: $40 in 10min")
    assert not r.should_accept_task("any-agent", "any-wf")
    assert not r.allow_high_impact()
    text = alert.read_text()
    assert "GLOBAL CIRCUIT BREAKER OPEN" in text and "cost spike" in text
    # reopening while already open must not duplicate the alert line
    r.open("global", GLOBAL_KEY, "cost spike: still going")
    assert alert.read_text().count("GLOBAL CIRCUIT BREAKER OPEN") == 1
    r.close_conn()


def test_breaker_events_on_bus(db):
    p, _ = db
    store = EventStore(p)
    bus = EventBus(store)
    r = BreakerRegistry(p, bus=bus)
    r.open("agent", "a1", "errors")
    r.close("agent", "a1")
    types = [e.type for _, e in store.read_since(0)]
    assert "breaker.opened" in types and "breaker.closed" in types
    r.close_conn(); store.close()


# ── heartbeat sweep ───────────────────────────────────────────────────────

def test_heartbeat_sweep_requeues_stale_agent_tasks(db):
    p, conn = db
    heartbeat(conn, "fresh-agent", "coder")
    conn.execute(
        "INSERT INTO agent_instances (id, role, status, last_heartbeat_at) VALUES (?,?,?,?)",
        ("dead-agent", "coder", "EXECUTING", _iso_ago(600)),
    )
    conn.execute(
        "INSERT INTO tasks (id, type, status, assigned_agent_id, updated_at) VALUES (?,?,?,?,?)",
        ("t1", "CODEGEN", "ASSIGNED", "dead-agent", _iso_ago(600)),
    )
    conn.commit()
    out = heartbeat_sweep(p, stale_after_s=120)
    assert out["stale_agents"] == ["dead-agent"]
    assert out["requeued_tasks"] == ["t1"]
    row = conn.execute("SELECT status, assigned_agent_id FROM tasks WHERE id='t1'").fetchone()
    assert row["status"] == "PENDING" and row["assigned_agent_id"] is None
    assert conn.execute("SELECT status FROM agent_instances WHERE id='dead-agent'").fetchone()["status"] == "UNRESPONSIVE"
    # fresh agent untouched
    assert conn.execute("SELECT status FROM agent_instances WHERE id='fresh-agent'").fetchone()["status"] == "IDLE"


# ── stuck task sweep ──────────────────────────────────────────────────────

def test_stuck_assigned_task_requeued_then_failed(db):
    p, conn = db
    conn.execute(
        "INSERT INTO tasks (id, type, status, assigned_agent_id, attempts, max_attempts, updated_at)"
        " VALUES ('t1','TEST','ASSIGNED','a1',0,2,?)", (_iso_ago(2000),))
    conn.commit()
    out = stuck_task_sweep(p, assigned_stale_s=900)
    assert out["requeued"] == ["t1"]
    # simulate it getting stuck again at attempts=1 -> exhausts (max 2)
    conn.execute("UPDATE tasks SET status='ASSIGNED', assigned_agent_id='a2', updated_at=? WHERE id='t1'",
                 (_iso_ago(2000),))
    conn.commit()
    out2 = stuck_task_sweep(p, assigned_stale_s=900)
    assert out2["failed"] == ["t1"]
    assert conn.execute("SELECT status FROM tasks WHERE id='t1'").fetchone()["status"] == "FAILED"


def test_waiting_dep_escalates_not_requeues(db):
    p, conn = db
    conn.execute(
        "INSERT INTO tasks (id, type, status, updated_at) VALUES ('t2','CODEGEN','WAITING_DEP',?)",
        (_iso_ago(7200),))
    conn.commit()
    out = stuck_task_sweep(p, waiting_stale_s=3600)
    assert out["escalated"] == ["t2"]
    assert conn.execute("SELECT status FROM tasks WHERE id='t2'").fetchone()["status"] == "WAITING_DEP"
    store = EventStore(p)
    assert any(e.type == "task.escalated" for _, e in store.read_since(0))
    store.close()


# ── metric watchdog ───────────────────────────────────────────────────────

def _emit_n(store, type, n, agg="agent-1"):
    for _ in range(n):
        store.append(EventEnvelope(type=type, aggregate_type="Session", aggregate_id=agg))


def test_agent_breaker_trips_on_failures(db):
    p, _ = db
    store = EventStore(p)
    _emit_n(store, "turn.failed", 6, agg="flaky")
    _emit_n(store, "turn.completed", 20, agg="healthy")
    out = metric_watchdog(p, agent_error_threshold=5)
    assert ("agent", "flaky") in out["tripped"]
    r = BreakerRegistry(p)
    assert r.is_open("agent", "flaky") and not r.is_open("agent", "healthy")
    r.close_conn(); store.close()


def test_global_breaker_trips_on_fail_ratio(db, tmp_path):
    p, _ = db
    store = EventStore(p)
    _emit_n(store, "task.failed", 8, agg="a")
    _emit_n(store, "task.completed", 4, agg="b")
    # patch default alert path away from the real hub
    import agentic_system.breakers as brk
    orig = brk._DEFAULT_ALERT
    brk._DEFAULT_ALERT = tmp_path / "ALERT.md"
    try:
        out = metric_watchdog(p, global_fail_ratio=0.5, global_min_events=10)
        assert ("global", GLOBAL_KEY) in out["tripped"]
        assert out["fail_ratio"] > 0.5
    finally:
        brk._DEFAULT_ALERT = orig
    store.close()


def test_watchdog_quiet_when_healthy(db):
    p, _ = db
    store = EventStore(p)
    _emit_n(store, "turn.completed", 30)
    out = metric_watchdog(p)
    assert out["tripped"] == [] and out["fail_ratio"] == 0.0
    store.close()


# ── daily consolidate ─────────────────────────────────────────────────────

def test_consolidate_archives_then_prunes(db, tmp_path):
    p, _ = db
    store = EventStore(p)
    store.append(EventEnvelope(type="ancient", created_at="2020-01-01T00:00:00Z"))
    store.append(EventEnvelope(type="recent"))
    out = daily_consolidate(p, retain_days=14, archive_dir=str(tmp_path / "arch"),
                            run_engraphis=False)
    assert out["pruned_events"] == 1
    assert "ancient" in open(out["archive"], encoding="utf-8").read()
    assert store.count() == 1
    store.close()
