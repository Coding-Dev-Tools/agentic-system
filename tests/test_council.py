"""Phase 4 tests: council fan-out, aggregation thresholds, cache, quorum."""

import json

import pytest

from agentic_system.council.schemas import CouncilRequest
from agentic_system.council.service import CouncilService
from agentic_system.events.bus import EventBus
from agentic_system.events.store import EventStore

MEMBERS = [
    {"id": "model-a", "weight": 1.1},
    {"id": "model-b", "weight": 1.0},
    {"id": "model-c", "weight": 0.9},
]


def _fake_llm(scores_by_model, rec_by_model, call_log=None):
    def fn(member, system, user):
        if call_log is not None:
            call_log.append(member.id)
        return json.dumps({
            "self_scores": scores_by_model[member.id],
            "recommendation": rec_by_model[member.id],
            "rationale": f"{member.id} says so",
        })
    return fn


def _request(content="diff --git a/x b/x\n+ok", risk="medium"):
    return CouncilRequest(subject_type="PR", content=content, risk_level=risk,
                          subject_ref={"repo": "demo", "commit": "abc123"},
                          correlation_id="wfr-1")


def _service(tmp_path, llm, bus=None, **kw):
    return CouncilService(str(tmp_path / "c.db"), bus=bus, members=MEMBERS,
                          peer_eval="never", llm_fn=llm, **kw)


GOOD = {"correctness": 5, "safety": 5, "style": 4, "tests": 4, "complexity": 4}
BAD = {"correctness": 2, "safety": 2, "style": 2, "tests": 2, "complexity": 2}
MID = {"correctness": 3, "safety": 4, "style": 3, "tests": 3, "complexity": 3}


def test_unanimous_approve(tmp_path):
    llm = _fake_llm({m["id"]: GOOD for m in MEMBERS},
                    {m["id"]: "approve" for m in MEMBERS})
    svc = _service(tmp_path, llm)
    d = svc.review(_request())
    assert d.decision == "APPROVE"
    assert d.metrics["agreement"] == 1.0
    assert set(d.per_model) == {"model-a", "model-b", "model-c"}
    svc.close()


def test_low_scores_reject(tmp_path):
    llm = _fake_llm({m["id"]: BAD for m in MEMBERS},
                    {m["id"]: "reject" for m in MEMBERS})
    svc = _service(tmp_path, llm)
    assert svc.review(_request()).decision == "REJECT"
    svc.close()


def test_disagreement_yields_rework(tmp_path):
    # scores decent but agreement below 0.7 -> REWORK, not APPROVE
    llm = _fake_llm({"model-a": GOOD, "model-b": GOOD, "model-c": GOOD},
                    {"model-a": "approve", "model-b": "rework", "model-c": "rework"})
    svc = _service(tmp_path, llm)
    d = svc.review(_request())
    assert d.decision == "REWORK"
    assert d.metrics["agreement"] < 0.7
    svc.close()


def test_safety_floor_blocks_approve(tmp_path):
    low_safety = dict(GOOD, safety=3)
    llm = _fake_llm({m["id"]: low_safety for m in MEMBERS},
                    {m["id"]: "approve" for m in MEMBERS})
    svc = _service(tmp_path, llm)
    assert svc.review(_request()).decision == "REWORK"
    svc.close()


def test_verdict_cache_zero_tokens_on_identical_subject(tmp_path):
    calls = []
    llm = _fake_llm({m["id"]: GOOD for m in MEMBERS},
                    {m["id"]: "approve" for m in MEMBERS}, call_log=calls)
    svc = _service(tmp_path, llm)
    d1 = svc.review(_request())
    n = len(calls)
    d2 = svc.review(_request())          # identical diff + rubric
    assert d2.cached and d2.decision == d1.decision
    assert len(calls) == n               # zero new model calls
    d3 = svc.review(_request(content="different diff"))
    assert not d3.cached and len(calls) > n
    svc.close()


def test_malformed_member_loses_vote_but_council_proceeds(tmp_path):
    def llm(member, system, user):
        if member.id == "model-b":
            return "I refuse to answer in JSON."
        return json.dumps({"self_scores": GOOD, "recommendation": "approve",
                           "rationale": "ok"})
    svc = _service(tmp_path, llm)
    d = svc.review(_request())
    assert d.decision == "APPROVE"
    assert "model-b" not in d.per_model
    svc.close()


def test_quorum_failure_is_rework_never_approve(tmp_path):
    def llm(member, system, user):
        if member.id == "model-a":
            return json.dumps({"self_scores": GOOD, "recommendation": "approve",
                               "rationale": "ok"})
        return "garbage" if member.id == "model-b" else "also garbage"
    svc = _service(tmp_path, llm, min_quorum=2)
    d = svc.review(_request())
    assert d.decision == "REWORK" and "insufficient_quorum" in d.reason
    svc.close()


def test_peer_eval_runs_for_high_risk(tmp_path):
    stage2_calls = []

    def llm(member, system, user):
        if "Reviews to evaluate" in user:  # stage-2 prompt
            stage2_calls.append(member.id)
            return json.dumps({"scores": [
                {"review_model_id": m["id"], "overall": 4.5, "justification": "solid"}
                for m in MEMBERS]})
        return json.dumps({"self_scores": GOOD, "recommendation": "approve",
                           "rationale": "ok"})

    svc = CouncilService(str(tmp_path / "c.db"), members=MEMBERS,
                         peer_eval="high_risk_only", llm_fn=llm)
    d = svc.review(_request(risk="high"))
    assert d.decision == "APPROVE" and len(stage2_calls) == 3
    stage2_calls.clear()
    d2 = svc.review(_request(content="other diff", risk="medium"))
    assert d2.decision == "APPROVE" and stage2_calls == []
    svc.close()


def test_decision_event_emitted_and_session_persisted(tmp_path):
    store = EventStore(str(tmp_path / "c.db"))
    bus = EventBus(store)
    llm = _fake_llm({m["id"]: GOOD for m in MEMBERS},
                    {m["id"]: "approve" for m in MEMBERS})
    svc = _service(tmp_path, llm, bus=bus)
    d = svc.review(_request())
    types = [e.type for _, e in store.read_since(0)]
    assert "council.decision" in types
    row = svc._conn.execute("SELECT * FROM council_sessions WHERE id=?",
                            (d.session_id,)).fetchone()
    assert row["decision"] == "APPROVE" and row["status"] == "DONE"
    session = json.loads(row["session_json"])
    assert set(session["reviews"]) == {"model-a", "model-b", "model-c"}
    svc.close(); store.close()


def test_persist_hook_receives_full_session(tmp_path):
    seen = {}

    def hook(doc):
        seen.update(doc)
        return "mem-verdict-1"

    llm = _fake_llm({m["id"]: GOOD for m in MEMBERS},
                    {m["id"]: "approve" for m in MEMBERS})
    svc = _service(tmp_path, llm, persist_hook=hook)
    d = svc.review(_request())
    assert seen["decision"] == "APPROVE" and seen["correlation_id"] == "wfr-1"
    row = svc._conn.execute("SELECT engraphis_ref FROM council_sessions WHERE id=?",
                            (d.session_id,)).fetchone()
    assert row["engraphis_ref"] == "mem-verdict-1"
    svc.close()


def test_make_engraphis_persist_hook_real_round_trip(tmp_path, monkeypatch):
    """Regression: the hook must build a durable MemoryService (via create(),
    not the nonexistent create_default()) and actually persist a verdict to
    Engraphis with kind=council_verdict. Skips when engraphis isn't importable."""
    pytest.importorskip("engraphis")
    db = tmp_path / "engraphis_hook.db"
    monkeypatch.setenv("ENGRAPHIS_DB_PATH", str(db))
    from agentic_system.council.service import make_engraphis_persist_hook
    hook = make_engraphis_persist_hook()
    # if engraphis import failed the hook degrades to a no-op lambda — skip
    if hook.__name__ != "hook":
        pytest.skip("engraphis persist hook is the no-op fallback")
    ref = hook({"session_id": "council-rt", "decision": "APPROVE",
                "subject_type": "PR", "subject_ref": {"run": "r"},
                "metrics": {"agreement": 1.0}, "correlation_id": "r",
                "session": {"member_ids": ["m"]}})
    assert ref and ref.startswith("mem_"), f"hook returned {ref!r}"
    # verify it landed durably with the right provenance
    from engraphis.service import MemoryService
    from engraphis.config import settings
    svc = MemoryService.create(settings.db_path, embed_model=settings.embed_model or None)
    try:
        rec = svc.store.get_memory(ref)
        assert rec is not None
        assert rec.title == "Council APPROVE: PR council-rt"
        assert str(rec.mtype) == "MemoryType.EPISODIC"
        assert str(rec.scope) == "Scope.WORKSPACE"
        prov = (rec.metadata or {}).get("provenance", {})
        assert prov.get("kind") == "council_verdict"
        assert prov.get("source") == "agent:council"
    finally:
        svc.store.close()
