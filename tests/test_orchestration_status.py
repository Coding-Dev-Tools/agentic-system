"""Wiring test: agent.orchestration_status read-only status surface."""

import json

import pytest

import agentic_system.orchestration_status as st
from agentic_system.breakers import GLOBAL_KEY, BreakerRegistry
from agentic_system.events import hooks as orch_hooks
from agentic_system.events.state_tables import connect, ensure_state_tables, heartbeat, now_iso


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = str(tmp_path / "events.db")
    monkeypatch.setenv("HERMES_EVENTS_DB", p)
    monkeypatch.setenv("HERMES_ORCHESTRATION", "1")
    orch_hooks.reset_bus_for_tests()
    yield p
    orch_hooks.reset_bus_for_tests()


def _seed(p):
    class A:
        session_id = "s1"; model = "m"; provider = "p"
    orch_hooks.emit_turn_started(A(), task_id="T-1")
    orch_hooks.emit_turn_completed(A(), task_id="T-1", api_calls=2, total_tokens=300)
    br = BreakerRegistry(p, bus=orch_hooks.get_bus())
    br.open("global", GLOBAL_KEY, "cost spike")
    br.close_conn()
    c = connect(p); ensure_state_tables(c)
    heartbeat(c, "agent-1", role="coder", status="EXECUTING")
    c.execute(
        "INSERT INTO tasks (id,type,status,assigned_agent_id,attempts,max_attempts,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("T-1", "CODEGEN", "ASSIGNED", "agent-1", 1, 3, now_iso(), now_iso()))
    c.commit(); c.close()


def test_missing_db_reports_not_exists(tmp_path):
    s = st.collect(str(tmp_path / "absent.db"))
    assert s["exists"] is False
    assert "nothing has run" in s["note"]


def test_empty_db_does_not_crash(tmp_path):
    p = tmp_path / "empty.db"
    p.write_text("")
    s = st.collect(str(p))
    assert s["exists"] is True
    assert s["total_events"] == 0
    assert s["breakers"] == []
    assert s["breaker_any_open"] is False


def test_seeded_snapshot(db):
    _seed(db)
    s = st.collect(db, tail=10)
    assert s["exists"] is True
    assert s["total_events"] >= 3
    assert any(b["level"] == "global" and b["state"] == "OPEN" for b in s["breakers"])
    assert s["breaker_any_open"] is True
    assert any(a["id"] == "agent-1" and a["status"] == "EXECUTING" for a in s["agents"])
    assert any(t["id"] == "T-1" and t["status"] == "ASSIGNED" for t in s["recent_tasks"])
    types = {e["type"] for e in s["recent_events"]}
    assert "turn.started" in types and "breaker.opened" in types


def test_human_output_mentions_open_breaker(db, capsys):
    _seed(db)
    rc = st.main(["--db", db, "--tail", "5"])
    out = capsys.readouterr().out
    assert "OPEN" in out
    assert "high-impact tools blocked" in out
    assert "agent-1" in out
    assert rc == 1  # OPEN breaker -> non-zero for health checks


def test_exit_zero_when_no_breaker_open(db, capsys):
    _seed(db)
    # close the breaker we opened
    br = BreakerRegistry(db); br.close("global", GLOBAL_KEY, "ok"); br.close_conn()
    rc = st.main(["--db", db, "--tail", "2"])
    assert rc == 0


def test_json_mode_is_valid(db, capsys):
    _seed(db)
    rc = st.main(["--db", db, "--json", "--tail", "3"])
    out = capsys.readouterr().out
    d = json.loads(out)
    assert d["exists"] is True
    assert d["breaker_any_open"] is True
    assert "recent_events" in d
    assert rc == 1


def test_missing_db_cli_exit_zero(tmp_path, capsys):
    rc = st.main(["--db", str(tmp_path / "nope.db")])
    assert rc == 0
    assert "nothing has run" in capsys.readouterr().out