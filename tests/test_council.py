"""Phase 4 tests: council fan-out, aggregation thresholds, cache, quorum."""

import json
import time

import pytest

from agentic_system.council import CouncilRequest, DimensionPolicy, GatePolicy
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


def test_cached_decision_preserves_original_reason(tmp_path):
    llm = _fake_llm(
        {member["id"]: GOOD for member in MEMBERS},
        {member["id"]: "rework" for member in MEMBERS},
    )
    svc = _service(tmp_path, llm)
    request = _request(content="cache a rework reason")

    first = svc.review(request)
    cached = svc.review(request)

    assert first.decision == "REWORK"
    assert first.reason == "policy_checks_failed: agreement"
    assert cached.cached is True
    assert cached.reason == first.reason
    row = svc._conn.execute(
        "SELECT session_json FROM council_sessions WHERE id=?",
        (first.session_id,),
    ).fetchone()
    assert json.loads(row["session_json"])["reason"] == first.reason
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
    assert d.per_model["model-b"]["call"]["status"] == "invalid_output"
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


def test_peer_outcome_is_reported_when_member_review_failed(tmp_path):
    def llm(member, system, user):
        if "Reviews to evaluate" in user:
            return json.dumps({"scores": [
                {
                    "review_model_id": model_id,
                    "overall": 5,
                    "justification": "sound",
                }
                for model_id in ("model-a", "model-b")
            ]})
        if member.id == "model-c":
            raise ConnectionError("review unavailable")
        return json.dumps({
            "self_scores": GOOD,
            "recommendation": "approve",
            "rationale": "ok",
        })

    svc = CouncilService(
        str(tmp_path / "peer-outcomes.db"),
        members=MEMBERS,
        peer_eval="always",
        min_quorum=2,
        llm_fn=llm,
    )
    decision = svc.review(_request(content="peer outcome coverage"))

    assert decision.decision == "APPROVE"
    assert decision.per_model["model-c"]["call"]["status"] == "provider_error"
    assert decision.per_model["model-c"]["peer_call"]["status"] == "success"
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


def test_unexpected_review_failure_marks_session_failed(tmp_path, monkeypatch):
    svc = _service(tmp_path, lambda member, system, user: "{}")

    def fail_stage(request, deadline):
        raise RuntimeError("review runner failed")

    monkeypatch.setattr(svc, "_stage1", fail_stage)
    with pytest.raises(RuntimeError, match="review runner failed"):
        svc.review(_request(content="unexpected failure"))

    row = svc._conn.execute(
        "SELECT status, session_json FROM council_sessions",
    ).fetchone()
    assert row["status"] == "FAILED"
    assert json.loads(row["session_json"]) == {"error": "RuntimeError"}
    svc.close()


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
    # Ensure no workspace restriction leaks from the host environment
    monkeypatch.delenv("ENGRAPHIS_WORKSPACES", raising=False)
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


def test_review_deadline_does_not_wait_for_legacy_adapter(tmp_path):
    def slow_llm(member, system, user):
        time.sleep(0.3)
        return json.dumps({
            "self_scores": GOOD,
            "recommendation": "approve",
            "rationale": "late",
        })

    svc = _service(tmp_path, slow_llm, review_timeout_seconds=0.04)
    started = time.monotonic()
    decision = svc.review(_request())
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert decision.decision == "REWORK"
    assert {
        result["call"]["status"] for result in decision.per_model.values()
    } == {"timeout"}
    svc.close()


def test_deadline_is_forwarded_to_cooperative_adapter(tmp_path):
    observed_timeouts = []

    def deadline_llm(member, system, user, *, timeout_seconds):
        observed_timeouts.append(timeout_seconds)
        raise TimeoutError("provider deadline")

    svc = _service(tmp_path, deadline_llm, review_timeout_seconds=0.2)
    decision = svc.review(_request())

    assert decision.decision == "REWORK"
    assert len(observed_timeouts) == len(MEMBERS)
    assert all(0 < value <= 0.21 for value in observed_timeouts)
    assert {
        result["call"]["status"] for result in decision.per_model.values()
    } == {"timeout"}
    svc.close()


def test_positional_only_timeout_name_remains_a_legacy_adapter(tmp_path):
    observed_timeouts = []

    def positional_llm(member, system, user, timeout_seconds=None, /):
        observed_timeouts.append(timeout_seconds)
        return json.dumps({
            "self_scores": GOOD,
            "recommendation": "approve",
            "rationale": "ok",
        })

    svc = _service(tmp_path, positional_llm)
    decision = svc.review(_request(content="positional timeout parameter"))

    assert decision.decision == "APPROVE"
    assert observed_timeouts == [None] * len(MEMBERS)
    svc.close()


def test_replacing_llm_adapter_refreshes_timeout_capability(tmp_path):
    def legacy_llm(member, system, user):
        raise AssertionError("replaced adapter must not be called")

    observed_timeouts = []

    def cooperative_llm(member, system, user, *, timeout_seconds):
        observed_timeouts.append(timeout_seconds)
        return json.dumps({
            "self_scores": GOOD,
            "recommendation": "approve",
            "rationale": "ok",
        })

    svc = _service(tmp_path, legacy_llm)
    svc.llm_fn = cooperative_llm
    decision = svc.review(_request(content="hot-swapped adapter"))

    assert decision.decision == "APPROVE"
    assert len(observed_timeouts) == len(MEMBERS)
    assert all(timeout > 0 for timeout in observed_timeouts)
    svc.close()


def test_peer_eval_uses_remaining_shared_deadline(tmp_path):
    review_timeouts = []
    peer_timeouts = []

    def cooperative_llm(member, system, user, *, timeout_seconds):
        if "Reviews to evaluate" in user:
            peer_timeouts.append(timeout_seconds)
            return json.dumps({"scores": [
                {
                    "review_model_id": model["id"],
                    "overall": 5,
                    "justification": "sound",
                }
                for model in MEMBERS
            ]})
        review_timeouts.append(timeout_seconds)
        time.sleep(0.05)
        return json.dumps({
            "self_scores": GOOD,
            "recommendation": "approve",
            "rationale": "ok",
        })

    svc = CouncilService(
        str(tmp_path / "shared-deadline.db"),
        members=MEMBERS,
        peer_eval="always",
        llm_fn=cooperative_llm,
        review_timeout_seconds=1.0,
    )
    decision = svc.review(_request(content="shared deadline"))

    assert decision.decision == "APPROVE"
    assert len(review_timeouts) == len(MEMBERS)
    assert len(peer_timeouts) == len(MEMBERS)
    assert max(peer_timeouts) < min(review_timeouts) - 0.02
    svc.close()


def test_security_gate_honors_lower_is_better_dimensions(tmp_path):
    secure = {
        "vulnerability_severity": 1,
        "exploitability": 1,
        "fix_correctness": 5,
        "blast_radius": 1,
        "compliance": 5,
    }
    calls = []
    llm = _fake_llm(
        {member["id"]: secure for member in MEMBERS},
        {member["id"]: "approve" for member in MEMBERS},
        call_log=calls,
    )
    svc = _service(tmp_path, llm)
    request = CouncilRequest(
        subject_type="SECURITY_REVIEW",
        subject_ref={"repo": "demo"},
        content="scanner evidence",
        gate="security",
    )

    approved = svc.review(request)
    assert approved.decision == "APPROVE"
    assert approved.gate == "security"

    risky = dict(secure, vulnerability_severity=5)
    svc.llm_fn = _fake_llm(
        {member["id"]: risky for member in MEMBERS},
        {member["id"]: "approve" for member in MEMBERS},
    )
    rejected_gate = svc.review(request.model_copy(update={"content": "new scan"}))
    assert rejected_gate.decision == "REWORK"
    assert rejected_gate.metrics["pass_vulnerability_severity"] == 0.0
    svc.close()


def test_merge_gate_requires_and_enforces_host_evidence(tmp_path):
    model_scores = {
        "correctness": 5,
        "safety": 5,
        "tests": 5,
        "ci_status": 5,
        "branch_protection": 5,
    }
    llm = _fake_llm(
        {member["id"]: model_scores for member in MEMBERS},
        {member["id"]: "approve" for member in MEMBERS},
    )
    svc = _service(tmp_path, llm)
    request = CouncilRequest(
        subject_type="MERGE",
        subject_ref={"repo": "demo", "pr": 7},
        content="merge candidate",
        gate="merge",
    )

    with pytest.raises(ValueError, match="host-derived evidence_scores"):
        svc.review(request)

    decision = svc.review(request.model_copy(update={
        "evidence_scores": {"ci_status": 1, "branch_protection": 5},
    }))
    assert decision.decision == "REWORK"
    assert decision.metrics["avg_ci_status"] == 1
    assert decision.metrics["pass_ci_status"] == 0.0
    svc.close()


def test_custom_gate_policy_is_deterministic(tmp_path):
    policy = GatePolicy(
        name=" latency ",
        dimensions=(
            DimensionPolicy(
                name="latency_risk", direction="lower", approve_at=2),
            DimensionPolicy(
                name="operability", direction="higher", approve_at=4),
        ),
    )
    scores = {"latency_risk": 1, "operability": 5}
    llm = _fake_llm(
        {member["id"]: scores for member in MEMBERS},
        {member["id"]: "approve" for member in MEMBERS},
    )
    svc = _service(tmp_path, llm)
    decision = svc.review(CouncilRequest(
        subject_type="DEPLOYMENT",
        content="deployment plan",
        gate_policy=policy,
    ))

    assert decision.decision == "APPROVE"
    assert decision.gate == "latency"
    assert decision.metrics["pass_latency_risk"] == 1.0
    svc.close()


def test_cache_fingerprint_includes_member_weights(tmp_path):
    db_path = str(tmp_path / "cache.db")
    calls = []
    llm = _fake_llm(
        {member["id"]: GOOD for member in MEMBERS},
        {member["id"]: "approve" for member in MEMBERS},
        call_log=calls,
    )
    first = CouncilService(
        db_path, members=MEMBERS, peer_eval="never", llm_fn=llm)
    assert not first.review(_request()).cached
    first.close()

    changed_members = [dict(member) for member in MEMBERS]
    changed_members[0]["weight"] = 2.0
    second = CouncilService(
        db_path, members=changed_members, peer_eval="never", llm_fn=llm)
    second_decision = second.review(_request())

    assert not second_decision.cached
    assert len(calls) == 2 * len(MEMBERS)
    second.close()


def test_out_of_range_scores_are_invalid_output(tmp_path):
    invalid = dict(GOOD, correctness=6)
    llm = _fake_llm(
        {member["id"]: invalid for member in MEMBERS},
        {member["id"]: "approve" for member in MEMBERS},
    )
    svc = _service(tmp_path, llm)
    decision = svc.review(_request())

    assert decision.decision == "REWORK"
    assert {
        result["call"]["status"] for result in decision.per_model.values()
    } == {"invalid_output"}
    svc.close()


def test_degraded_decision_is_not_cached(tmp_path):
    db_path = str(tmp_path / "degraded-cache.db")

    def unavailable(member, system, user):
        raise ConnectionError("offline")

    first = CouncilService(
        db_path, members=MEMBERS, peer_eval="never", llm_fn=unavailable)
    degraded = first.review(_request())
    assert degraded.decision == "REWORK"
    first.close()

    calls = []
    recovered_llm = _fake_llm(
        {member["id"]: GOOD for member in MEMBERS},
        {member["id"]: "approve" for member in MEMBERS},
        call_log=calls,
    )
    recovered = CouncilService(
        db_path, members=MEMBERS, peer_eval="never", llm_fn=recovered_llm)
    decision = recovered.review(_request())

    assert not decision.cached
    assert decision.decision == "APPROVE"
    assert len(calls) == len(MEMBERS)
    recovered.close()


def test_model_response_size_is_bounded(tmp_path):
    def oversized(member, system, user):
        return json.dumps({
            "self_scores": GOOD,
            "recommendation": "approve",
            "rationale": "x" * 100,
        })

    svc = _service(tmp_path, oversized, max_model_response_chars=50)
    decision = svc.review(_request())

    assert decision.decision == "REWORK"
    assert {
        result["call"]["status"] for result in decision.per_model.values()
    } == {"invalid_output"}
    svc.close()


def test_request_rejects_non_json_evidence():
    with pytest.raises(ValueError, match="finite JSON data"):
        CouncilRequest(
            subject_type="PR",
            content="diff",
            evidence={"bad": object()},
        )
