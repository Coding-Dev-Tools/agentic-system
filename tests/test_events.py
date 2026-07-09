"""Phase 0 tests: envelope validation, store durability, bus delivery."""

import os
import threading
import time

import pytest

from agentic_system.events.envelope import EventEnvelope
from agentic_system.events.store import EventStore
from agentic_system.events.bus import EventBus


@pytest.fixture()
def store(tmp_path):
    s = EventStore(str(tmp_path / "events.db"))
    yield s
    s.close()


# ── envelope ──────────────────────────────────────────────────────────────

def test_envelope_defaults():
    e = EventEnvelope(type="TaskCreated")
    assert e.id.startswith("evt-")
    assert e.schema_version == "1.0"
    assert e.priority == "normal"
    assert e.created_at.endswith("Z")


def test_envelope_rejects_empty_type():
    with pytest.raises(Exception):
        EventEnvelope(type="   ")


def test_envelope_rejects_bad_priority():
    with pytest.raises(Exception):
        EventEnvelope(type="x", priority="urgent")


def test_envelope_rejects_unknown_fields():
    with pytest.raises(Exception):
        EventEnvelope(type="x", bogus_field=1)


def test_envelope_causation_chain():
    parent = EventEnvelope(type="TaskCreated", correlation_id="wf-1")
    child = EventEnvelope(type="TaskClaimed").caused_by(parent)
    assert child.causation_id == parent.id
    assert child.correlation_id == "wf-1"


# ── store ─────────────────────────────────────────────────────────────────

def test_append_and_read_ordering(store):
    for i in range(5):
        store.append(EventEnvelope(type=f"t{i}", aggregate_type="Task", aggregate_id="a"))
    got = store.read_since(0)
    assert [e.type for _, e in got] == ["t0", "t1", "t2", "t3", "t4"]
    assert store.last_seq() == 5


def test_read_since_filters_types(store):
    store.append(EventEnvelope(type="keep"))
    store.append(EventEnvelope(type="drop"))
    store.append(EventEnvelope(type="keep"))
    got = store.read_since(0, types=["keep"])
    assert len(got) == 2


def test_replay_by_aggregate_and_correlation(store):
    store.append(EventEnvelope(type="a", aggregate_type="Task", aggregate_id="t1", correlation_id="c1"))
    store.append(EventEnvelope(type="b", aggregate_type="Task", aggregate_id="t2", correlation_id="c1"))
    assert [e.type for e in store.read_for("Task", "t1")] == ["a"]
    assert [e.type for e in store.read_correlation("c1")] == ["a", "b"]


def test_offsets_roundtrip(store):
    assert store.get_offset("c") == 0
    store.commit_offset("c", 7)
    assert store.get_offset("c") == 7
    store.commit_offset("c", 9)
    assert store.get_offset("c") == 9


def test_payload_survives_roundtrip(store):
    payload = {"nested": {"a": [1, 2, 3]}, "s": "héllo"}
    store.append(EventEnvelope(type="x", payload=payload))
    _, e = store.read_since(0)[0]
    assert e.payload == payload


def test_prune_before_archives(store, tmp_path):
    store.append(EventEnvelope(type="old", created_at="2020-01-01T00:00:00Z"))
    store.append(EventEnvelope(type="new"))
    archive = tmp_path / "arch" / "events.jsonl"
    n = store.prune_before("2025-01-01T00:00:00Z", archive_path=str(archive))
    assert n == 1
    assert store.count() == 1
    assert archive.exists() and "old" in archive.read_text()


def test_store_survives_reopen(tmp_path):
    p = str(tmp_path / "e.db")
    s1 = EventStore(p)
    s1.append(EventEnvelope(type="persisted"))
    s1.close()
    s2 = EventStore(p)
    assert s2.count() == 1
    s2.close()


# ── bus ───────────────────────────────────────────────────────────────────

def test_publish_notifies_listeners(store):
    bus = EventBus(store)
    seen = []
    bus.add_listener(lambda e: seen.append(e.type))
    bus.publish("x.y", {"k": 1}, correlation_id="c9")
    assert seen == ["x.y"]
    assert store.count() == 1


def test_listener_error_does_not_break_publish(store):
    bus = EventBus(store)
    bus.add_listener(lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    env = bus.publish("x.y")
    assert env.type == "x.y"
    assert store.count() == 1


def test_subscribe_delivers_and_commits_offset(store):
    bus = EventBus(store)
    got = []
    done = threading.Event()

    def handler(e):
        got.append(e.type)
        if len(got) == 3:
            done.set()

    sub = bus.subscribe("worker", handler, poll_interval=0.05)
    for i in range(3):
        bus.publish(f"e{i}")
    assert done.wait(5.0), "consumer did not receive events in time"
    sub.stop()
    assert got == ["e0", "e1", "e2"]
    assert store.get_offset("worker") == 3


def test_subscribe_resumes_from_offset(store):
    bus = EventBus(store)
    bus.publish("before")
    store.commit_offset("w2", store.last_seq())  # already consumed
    got = []
    evt = threading.Event()
    sub = bus.subscribe("w2", lambda e: (got.append(e.type), evt.set()), poll_interval=0.05)
    bus.publish("after")
    assert evt.wait(5.0)
    sub.stop()
    assert got == ["after"]


def test_poison_event_skipped_after_max_failures(store):
    bus = EventBus(store)
    calls = {"n": 0}
    ok = threading.Event()

    def handler(e):
        if e.type == "poison":
            calls["n"] += 1
            raise RuntimeError("cannot handle")
        ok.set()

    bus.publish("poison")
    bus.publish("good")
    sub = bus.subscribe("w3", handler, poll_interval=0.02)
    assert ok.wait(10.0), "good event never delivered — poison not skipped"
    sub.stop()
    assert calls["n"] >= 5  # retried then skipped


# ── hooks (flag gating) ───────────────────────────────────────────────────

def test_hooks_disabled_by_default(monkeypatch, tmp_path):
    from agentic_system.events import hooks
    monkeypatch.setenv("HERMES_ORCHESTRATION", "0")
    hooks.reset_bus_for_tests()
    assert hooks.get_bus() is None
    hooks.emit("anything")  # must not raise


def test_hooks_enabled_via_env(monkeypatch, tmp_path):
    from agentic_system.events import hooks
    db = str(tmp_path / "hooks.db")
    monkeypatch.setenv("HERMES_ORCHESTRATION", "1")
    monkeypatch.setenv("HERMES_EVENTS_DB", db)
    hooks.reset_bus_for_tests()
    bus = hooks.get_bus()
    assert bus is not None
    hooks.emit("turn.started", {"session_id": "s1"})
    assert bus.store.count() == 1
    hooks.reset_bus_for_tests()
