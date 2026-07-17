"""Pydantic schemas for the Model Council + 8 Specialized Gates.

Structured in/out at every boundary: model responses are parsed into
ModelReview and REJECTED if malformed — a council member that cannot produce
valid JSON simply doesn't get a vote (hallucination guard).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

RECOMMENDATIONS = ("approve", "approve_with_nits", "rework", "reject")
_APPROVING = ("approve", "approve_with_nits")

DEFAULT_DIMENSIONS = ("correctness", "safety", "style", "tests", "complexity")

# Gate-specific dimension presets
GATE_DIMENSIONS = {
    "code_edit": ("correctness", "safety", "style", "tests", "minimal_change"),
    "pr_review": ("correctness", "safety", "tests", "documentation", "scope"),
    "merge": ("correctness", "safety", "tests", "ci_status", "branch_protection"),
    "delegation": ("feasibility", "clarity", "risk", "dependencies", "value"),
    "security": ("vulnerability_severity", "exploitability", "fix_correctness", "blast_radius", "compliance"),
    "code_quality": ("complexity", "duplication", "test_coverage", "documentation", "maintainability"),
    "dependency": ("vulnerability", "license_compliance", "version_freshness", "supply_chain", "breaking_changes"),
    "architecture": ("cohesion", "coupling", "scalability", "observability", "evolution"),
}


class CouncilMember(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    id: str                       # model id, e.g. "claude-sonnet-5"
    provider: Optional[str] = None
    weight: float = 1.0
    base_url: Optional[str] = None   # for custom/OpenAI-compatible providers
    api_key: Optional[str] = None    # paired with base_url

    @field_validator("weight")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("weight must be > 0")
        return v


class CouncilThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_overall: float = 4.0
    min_safety: float = 4.5
    min_tests: float = 3.5
    min_agreement: float = 0.7
    reject_max_overall: float = 2.5


class CouncilRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_type: str                       # PR | REFACTOR | DEPLOYMENT | ESCALATION | CODE_EDIT | ...
    subject_ref: dict[str, Any] = Field(default_factory=dict)
    content: str                            # the diff / artifact under review
    artifact_refs: dict[str, Any] = Field(default_factory=dict)  # Engraphis ids
    rubric_dimensions: tuple[str, ...] = DEFAULT_DIMENSIONS
    scale_min: int = 1
    scale_max: int = 5
    decision_type: str = "REVIEW"
    risk_level: str = "medium"               # low | medium | high
    checklist: tuple[str, ...] = ()
    correlation_id: Optional[str] = None
    gate: Optional[str] = None               # code_edit | pr_review | merge | delegation | security | code_quality | dependency | architecture

    @field_validator("risk_level")
    @classmethod
    def _risk(cls, v: str) -> str:
        if v not in ("low", "medium", "high"):
            raise ValueError("risk_level must be low|medium|high")
        return v

    def subject_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def rubric_hash(self) -> str:
        basis = json.dumps({
            "dims": list(self.rubric_dimensions),
            "scale": [self.scale_min, self.scale_max],
            "decision_type": self.decision_type,
            "checklist": list(self.checklist),
            "gate": self.gate,
        }, sort_keys=True)
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

    def effective_dimensions(self) -> tuple[str, ...]:
        if self.gate and self.gate in GATE_DIMENSIONS:
            return GATE_DIMENSIONS[self.gate]
        return self.rubric_dimensions


class ModelReview(BaseModel):
    model_config = ConfigDict(extra="ignore", protected_namespaces=())
    model_id: str = ""
    self_scores: dict[str, float]
    recommendation: str
    rationale: str = ""

    @field_validator("recommendation")
    @classmethod
    def _rec(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in RECOMMENDATIONS:
            raise ValueError(f"recommendation must be one of {RECOMMENDATIONS}")
        return v

    @property
    def approves(self) -> bool:
        return self.recommendation in _APPROVING

    @property
    def self_overall(self) -> float:
        vals = list(self.self_scores.values())
        return sum(vals) / len(vals) if vals else 0.0


class PeerScore(BaseModel):
    model_config = ConfigDict(extra="ignore")
    review_model_id: str
    overall: float
    justification: str = ""


class PeerEval(BaseModel):
    model_config = ConfigDict(extra="ignore")
    evaluator_model_id: str = ""
    scores: list[PeerScore] = Field(default_factory=list)


class CouncilDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(default_factory=lambda: f"council-{uuid.uuid4()}")
    decision: str                            # APPROVE | REWORK | REJECT
    metrics: dict[str, float] = Field(default_factory=dict)
    per_model: dict[str, Any] = Field(default_factory=dict)
    cached: bool = False
    reason: str = ""
    gate: Optional[str] = None

    @field_validator("decision")
    @classmethod
    def _dec(cls, v: str) -> str:
        if v not in ("APPROVE", "REWORK", "REJECT"):
            raise ValueError("decision must be APPROVE|REWORK|REJECT")
        return v


__all__ = ["CouncilMember", "CouncilThresholds", "CouncilRequest", "ModelReview",
           "PeerScore", "PeerEval", "CouncilDecision", "DEFAULT_DIMENSIONS",
           "RECOMMENDATIONS", "GATE_DIMENSIONS"]