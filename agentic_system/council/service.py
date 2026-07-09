"""Model Council service: parallel reviews -> weighted decision.

Stage 1: fan the same structured-review prompt across N configured council
members in parallel (reusing the auxiliary call_llm() path, which already
handles provider resolution, credentials, fallback and retry).
Stage 2 (optional, default risk_level=high only): peer evaluation — each
member scores the anonymized stage-1 reviews (NAACL'25-style).
Stage 3: deterministic weighted aggregation -> APPROVE / REWORK / REJECT.

Guards & economics:
- Malformed model output = no vote (pydantic-validated, one repair retry).
- Verdict cache: same subject_hash + rubric_hash + member set -> previous
  decision replayed, zero tokens.
- Quorum: fewer than ``min_quorum`` valid reviews -> REWORK (never APPROVE
  on thin evidence).
- The full session (reviews, peer scores, metrics) is persisted to
  council_sessions and, via ``persist_hook``, to Engraphis namespace
  ``hermes-council`` (kind=council_verdict) for why/timeline audit.

Config (your config file)::

    council:
      members:
        - {id: "claude-sonnet-5", provider: "anthropic", weight: 1.1}
        - {id: "gpt-4o", provider: "openai", weight: 1.0}
        - {id: "gemini-2.5-pro", provider: "openrouter", weight: 0.9}
      thresholds: {min_overall: 4.0, min_safety: 4.5, min_tests: 3.5,
                   min_agreement: 0.7, reject_max_overall: 2.5}
      peer_eval: high_risk_only   # never | high_risk_only | always
      min_quorum: 2
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional

from agentic_system.council.schemas import (
    CouncilDecision, CouncilMember, CouncilRequest, CouncilThresholds,
    ModelReview, PeerEval,
)
from agentic_system.events.state_tables import connect, ensure_state_tables, now_iso

logger = logging.getLogger("agentic_system.council")

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

REVIEW_SYSTEM = (
    "You are one member of a code-review council. Review the artifact strictly "
    "against the rubric. Respond with ONLY a JSON object, no prose, matching:\n"
    '{"self_scores": {<dimension>: <number {lo}-{hi}>, ...}, '
    '"recommendation": "approve|approve_with_nits|rework|reject", '
    '"rationale": "<short reason>"}\n'
    "Score every rubric dimension. Be strict about safety and tests."
)

PEER_SYSTEM = (
    "You are evaluating anonymized council reviews of the same artifact. "
    "Respond with ONLY JSON: "
    '{"scores": [{"review_model_id": "<id>", "overall": <number {lo}-{hi}>, '
    '"justification": "<short>"}]}'
)


def _default_llm_fn(member: CouncilMember, system: str, user: str) -> str:
    """Default council LLM call, delegated to the host-registered LLMPort
    (``set_default_llm_fn``). A host wires its provider/credential path here;
    tests inject a fake. Raises a clear error if no host LLM is registered —
    callers can always bypass this by passing ``llm_fn=`` to CouncilService."""
    from agentic_system.ports import get_default_llm_fn
    return get_default_llm_fn()(member, system, user)


def _extract_json(text: str) -> dict[str, Any]:
    m = _JSON_RE.search(text or "")
    if not m:
        raise ValueError("no JSON object in model output")
    result = json.loads(m.group(0))
    assert isinstance(result, dict)
    return result


class CouncilService:
    def __init__(self, db_path: str, bus: Any = None,
                 members: Optional[list[dict]] = None,
                 thresholds: Optional[dict] = None,
                 peer_eval: Optional[str] = None,
                 min_quorum: Optional[int] = None,
                 llm_fn: Optional[Callable[[CouncilMember, str, str], str]] = None,
                 persist_hook: Optional[Callable[[dict], Optional[str]]] = None):
        self._conn = connect(db_path)
        ensure_state_tables(self._conn)
        self.bus = bus
        cfg_members, cfg_thresholds, cfg_peer, cfg_quorum = self._load_config()
        # Precedence: an explicit constructor argument wins over config; config
        # supplies the default when the argument is None. (Previously
        # min_quorum/peer_eval let config OVERRIDE the constructor arg, which
        # was inconsistent with members/thresholds -- now constructor wins
        # everywhere, the usual Python convention.)
        self.members = [CouncilMember(**m) for m in (members or cfg_members)]
        self.thresholds = CouncilThresholds(**(thresholds or cfg_thresholds))
        self.peer_eval = peer_eval or cfg_peer or "high_risk_only"
        self.min_quorum = int(min_quorum if min_quorum is not None
                             else (cfg_quorum if cfg_quorum is not None else 2))
        self.llm_fn = llm_fn or _default_llm_fn
        self.persist_hook = persist_hook

    @staticmethod
    def _load_config():
        """Council config via the ConfigPort seam (host-agnostic); silent
        fallback to defaults. Returns (members, thresholds, peer_eval, min_quorum)."""
        try:
            from agentic_system.ports import get_config_port
            cc = get_config_port().council_config()
            if not cc:
                return [], {}, None, None
            return (cc.get("members") or [], cc.get("thresholds") or {},
                    cc.get("peer_eval"), cc.get("min_quorum"))
        except Exception:
            return [], {}, None, None

    # ── main entry ───────────────────────────────────────────────────────
    def review(self, request: CouncilRequest) -> CouncilDecision:
        if not self.members:
            raise RuntimeError("council has no members configured "
                               "(council.members in config.yaml)")
        cached = self._cache_lookup(request)
        if cached is not None:
            self._emit("council.decision_cached", request, cached)
            return cached

        session_id = f"council-{uuid.uuid4()}"
        self._save_session(session_id, request, status="RUNNING")

        reviews = self._stage1(request)
        if len(reviews) < self.min_quorum:
            decision = CouncilDecision(
                session_id=session_id, decision="REWORK",
                reason=f"insufficient_quorum: {len(reviews)}/{self.min_quorum} "
                       f"valid reviews", per_model={m: r.model_dump() for m, r in reviews.items()},
            )
            self._finalize(session_id, request, reviews, {}, decision)
            return decision

        peer_evals: dict[str, PeerEval] = {}
        if self.peer_eval == "always" or (
                self.peer_eval == "high_risk_only" and request.risk_level == "high"):
            peer_evals = self._stage2(request, reviews)

        decision = self._aggregate(session_id, reviews, peer_evals)
        self._finalize(session_id, request, reviews, peer_evals, decision)
        return decision

    # ── stage 1: parallel structured reviews ─────────────────────────────
    def _stage1(self, request: CouncilRequest) -> dict[str, ModelReview]:
        lo, hi = request.scale_min, request.scale_max
        system = REVIEW_SYSTEM.replace("{lo}", str(lo)).replace("{hi}", str(hi))
        user = self._review_prompt(request)
        reviews: dict[str, ModelReview] = {}

        def one(member: CouncilMember) -> Optional[ModelReview]:
            prompt = user  # per-member copy; retry appends a repair note
            for attempt in (1, 2):  # one repair retry
                try:
                    raw = self.llm_fn(member, system, prompt)
                    data = _extract_json(raw)
                    data["model_id"] = member.id
                    review = ModelReview(**data)
                    missing = set(request.rubric_dimensions) - set(review.self_scores)
                    if missing:
                        raise ValueError(f"missing rubric dimensions: {missing}")
                    return review
                except Exception as exc:
                    logger.warning("council member %s attempt %d invalid: %s",
                                   member.id, attempt, exc)
                    if attempt == 1:
                        prompt = user + "\n\nYour previous reply was invalid " \
                                        f"({exc}). Return ONLY the JSON object."
            return None

        with ThreadPoolExecutor(max_workers=max(len(self.members), 1)) as pool:
            futs = {pool.submit(one, m): m for m in self.members}
            for fut in as_completed(futs):
                r = fut.result()
                if r is not None:
                    reviews[r.model_id] = r
        return reviews

    # ── stage 2: peer evaluation ─────────────────────────────────────────
    def _stage2(self, request: CouncilRequest,
                reviews: dict[str, ModelReview]) -> dict[str, PeerEval]:
        lo, hi = request.scale_min, request.scale_max
        system = PEER_SYSTEM.replace("{lo}", str(lo)).replace("{hi}", str(hi))
        anon = json.dumps([
            {"review_model_id": mid,
             "self_scores": r.self_scores,
             "recommendation": r.recommendation,
             "rationale": r.rationale}
            for mid, r in sorted(reviews.items())
        ], indent=1)
        user = (f"Artifact under review:\n{request.content[:6000]}\n\n"
                f"Reviews to evaluate:\n{anon}")
        evals: dict[str, PeerEval] = {}

        def one(member: CouncilMember) -> Optional[PeerEval]:
            try:
                data = _extract_json(self.llm_fn(member, system, user))
                data["evaluator_model_id"] = member.id
                return PeerEval(**data)
            except Exception as exc:
                logger.warning("peer eval by %s invalid: %s", member.id, exc)
                return None

        with ThreadPoolExecutor(max_workers=max(len(self.members), 1)) as pool:
            for fut in as_completed({pool.submit(one, m): m for m in self.members}):
                e = fut.result()
                if e is not None:
                    evals[e.evaluator_model_id] = e
        return evals

    # ── stage 3: deterministic weighted aggregation ─────────
    def _aggregate(self, session_id: str, reviews: dict[str, ModelReview],
                   peer_evals: dict[str, PeerEval]) -> CouncilDecision:
        weights = {m.id: m.weight for m in self.members}
        agg: dict[str, dict] = {}
        for mid, review in reviews.items():
            p_scores, p_weights = [], []
            for evaluator_id, ev in peer_evals.items():
                for s in ev.scores:
                    if s.review_model_id == mid:
                        w = weights.get(evaluator_id, 1.0)
                        p_scores.append(s.overall * w)
                        p_weights.append(w)
            peer_overall = (sum(p_scores) / sum(p_weights)) if p_weights else review.self_overall
            agg[mid] = {"peer_overall": peer_overall,
                        "self": review.self_scores,
                        "recommendation": review.recommendation,
                        "rationale": review.rationale}

        total_weight = sum(weights.get(mid, 1.0) for mid in reviews)
        approve_weight = sum(weights.get(mid, 1.0) for mid, r in reviews.items()
                             if reviews[mid].approves)
        overall = [a["peer_overall"] for a in agg.values()]
        safety = [r.self_scores.get("safety", 0.0) for r in reviews.values()]
        tests = [r.self_scores.get("tests", 0.0) for r in reviews.values()]
        avg_overall = sum(overall) / len(overall)
        avg_safety = sum(safety) / len(safety)
        avg_tests = sum(tests) / len(tests)
        agreement = approve_weight / total_weight if total_weight else 0.0

        th = self.thresholds
        if (avg_overall >= th.min_overall and avg_safety >= th.min_safety
                and avg_tests >= th.min_tests and agreement >= th.min_agreement):
            verdict = "APPROVE"
        elif avg_overall <= th.reject_max_overall:
            verdict = "REJECT"
        else:
            verdict = "REWORK"

        return CouncilDecision(
            session_id=session_id, decision=verdict,
            metrics={"avg_overall": round(avg_overall, 3),
                     "avg_safety": round(avg_safety, 3),
                     "avg_tests": round(avg_tests, 3),
                     "agreement": round(agreement, 3)},
            per_model=agg,
        )

    # ── persistence, cache, events ───────────────────────────────────────
    def _review_prompt(self, request: CouncilRequest) -> str:
        checklist = "\n".join(f"- {c}" for c in request.checklist) or "- (none)"
        return (f"Decision type: {request.decision_type} | "
                f"Risk: {request.risk_level} | Subject: {request.subject_type} "
                f"{json.dumps(request.subject_ref)}\n"
                f"Rubric dimensions: {', '.join(request.rubric_dimensions)} "
                f"(scale {request.scale_min}-{request.scale_max})\n"
                f"Checklist:\n{checklist}\n\n"
                f"ARTIFACT:\n{request.content}")

    def _cache_lookup(self, request: CouncilRequest) -> Optional[CouncilDecision]:
        row = self._conn.execute(
            """SELECT * FROM council_sessions
               WHERE subject_hash=? AND rubric_hash=? AND decision IS NOT NULL
               ORDER BY created_at DESC LIMIT 1""",
            (request.subject_hash(), request.rubric_hash()),
        ).fetchone()
        if row is None:
            return None
        try:
            session = json.loads(row["session_json"])
            member_ids = sorted(m.id for m in self.members)
            if sorted(session.get("member_ids", [])) != member_ids:
                return None  # council composition changed -> re-review
            return CouncilDecision(
                session_id=row["id"], decision=row["decision"],
                metrics=session.get("metrics", {}),
                per_model=session.get("per_model", {}),
                cached=True, reason="verdict_cache_hit",
            )
        except Exception:
            return None

    def _save_session(self, session_id: str, request: CouncilRequest,
                      status: str) -> None:
        ts = now_iso()
        self._conn.execute(
            """INSERT INTO council_sessions (id, subject_type, subject_ref,
                   subject_hash, rubric_hash, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                   updated_at=excluded.updated_at""",
            (session_id, request.subject_type, json.dumps(request.subject_ref),
             request.subject_hash(), request.rubric_hash(), status, ts, ts),
        )
        self._conn.commit()

    def _finalize(self, session_id: str, request: CouncilRequest,
                  reviews: dict[str, ModelReview], peer_evals: dict[str, PeerEval],
                  decision: CouncilDecision) -> None:
        session = {
            "member_ids": [m.id for m in self.members],
            "reviews": {mid: r.model_dump() for mid, r in reviews.items()},
            "peer_evals": {mid: e.model_dump() for mid, e in peer_evals.items()},
            "metrics": decision.metrics,
            "per_model": decision.per_model,
            "risk_level": request.risk_level,
            "decision_type": request.decision_type,
            "correlation_id": request.correlation_id,
        }
        engraphis_ref = None
        if self.persist_hook is not None:
            try:  # blackboard write is best-effort, never blocks the verdict
                engraphis_ref = self.persist_hook({
                    "session_id": session_id, "decision": decision.decision,
                    "subject_type": request.subject_type,
                    "subject_ref": request.subject_ref,
                    "metrics": decision.metrics, "session": session,
                    "correlation_id": request.correlation_id,
                })
            except Exception:
                logger.exception("council persist_hook failed")
        confidence = decision.metrics.get("agreement", 0.0)
        self._conn.execute(
            """UPDATE council_sessions SET status='DONE', decision=?,
                   confidence=?, session_json=?, engraphis_ref=?, updated_at=?
               WHERE id=?""",
            (decision.decision, confidence,
             json.dumps(session, ensure_ascii=False, default=str),
             engraphis_ref, now_iso(), session_id),
        )
        self._conn.commit()
        self._emit("council.decision", request, decision)

    def _emit(self, type: str, request: CouncilRequest,
              decision: CouncilDecision) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(
                type,
                {"session_id": decision.session_id, "decision": decision.decision,
                 "metrics": decision.metrics, "cached": decision.cached,
                 "subject_type": request.subject_type,
                 "risk_level": request.risk_level, "reason": decision.reason},
                aggregate_type="CouncilSession", aggregate_id=decision.session_id,
                correlation_id=request.correlation_id,
                priority="high" if decision.decision != "APPROVE" else "normal",
            )
        except Exception:
            logger.exception("council event emit failed")

    def close(self) -> None:
        self._conn.close()


def make_engraphis_persist_hook(namespace_workspace: str = "hermes-council"):
    """Returns a persist_hook writing verdicts to Engraphis (best-effort import;
    None-returning no-op when engraphis isn't installed in this environment).

    Mirrors the canonical ``MemoryService.create(...)`` construction in
    ``engraphis.mcp_server`` so the verdict lands in the same durable DB
    (``settings.db_path`` / ``ENGRAPHIS_DB_PATH``) as every other Engraphis
    write — NOT an in-memory DB. (Previously called a nonexistent
    ``create_default()``, which silently left ``svc=None`` so verdicts never
    persisted.)"""
    import logging
    log = logging.getLogger("agentic_system.council")
    try:
        from engraphis.service import MemoryService
        from engraphis.config import settings
    except Exception as e:
        log.warning("engraphis not available -- council verdict persistence disabled (%s)", e)
        return lambda doc: None
    try:
        svc = MemoryService.create(
            settings.db_path,
            embed_model=settings.embed_model or None,
            allowed_workspaces=settings.allowed_workspaces,
            extractor=settings.extractor,
        )
    except Exception as e:
        log.warning("engraphis MemoryService could not be built -- council verdict persistence disabled (%s)", e)
        return lambda doc: None

    def hook(doc: dict) -> Optional[str]:
        out = svc.remember(
            json.dumps(doc, ensure_ascii=False, default=str)[:90_000],
            workspace=namespace_workspace,
            mtype="episodic", scope="workspace",
            title=f"Council {doc['decision']}: {doc['subject_type']} {doc['session_id']}",
            source="agent:council", kind="council_verdict", importance=0.6,
        )
        result = out.get("id")
        return str(result) if result is not None else None

    return hook


__all__ = ["CouncilService", "make_engraphis_persist_hook"]
