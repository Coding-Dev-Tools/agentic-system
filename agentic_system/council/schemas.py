"""Pydantic schemas for the Model Council.

Structured in/out at every boundary: model responses are parsed into
ModelReview and REJECTED if malformed — a council member that cannot produce
valid JSON simply doesn't get a vote (hallucination guard).
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from types import MappingProxyType
from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RECOMMENDATIONS = ("approve", "approve_with_nits", "rework", "reject")
_APPROVING = ("approve", "approve_with_nits")
ScoreDirection = Literal["higher", "lower"]

DEFAULT_DIMENSIONS = ("correctness", "safety", "style", "tests", "complexity")

_RESERVED_DIMENSIONS = {"overall"}


def _validate_identifier(value: str, label: str) -> str:
    value = value.strip()
    valid_characters = all(
        character.isalnum() or character in "_.-"
        for character in value
    )
    if (
        not value
        or len(value) > 64
        or not value[0].isalpha()
        or not valid_characters
    ):
        raise ValueError(
            f"{label} must be a 1-64 character identifier")
    if label == "dimension name" and value in _RESERVED_DIMENSIONS:
        raise ValueError(f"{value!r} is a reserved dimension name")
    return value


class DimensionPolicy(BaseModel):
    """How one rubric dimension contributes to a gate decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    direction: ScoreDirection = "higher"
    approve_at: Optional[float] = None
    weight: float = 1.0

    @field_validator("name")
    @classmethod
    def _nonempty_name(cls, value: str) -> str:
        return _validate_identifier(value, "dimension name")

    @field_validator("approve_at")
    @classmethod
    def _finite_approval_threshold(cls, value: Optional[float]) -> Optional[float]:
        if value is not None and not math.isfinite(value):
            raise ValueError("approve_at must be finite")
        return value

    @field_validator("weight")
    @classmethod
    def _positive_weight(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("dimension weight must be finite and > 0")
        return value


class GatePolicy(BaseModel):
    """Deterministic aggregation policy for a named council gate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    dimensions: tuple[DimensionPolicy, ...]
    min_overall: float = 4.0
    min_agreement: float = 0.7
    reject_max_overall: float = 2.5
    required_evidence: tuple[str, ...] = ()

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        return _validate_identifier(value, "gate policy name")

    @model_validator(mode="after")
    def _valid_policy(self) -> "GatePolicy":
        names = [dimension.name for dimension in self.dimensions]
        if not names:
            raise ValueError("gate policy must define at least one dimension")
        if len(names) != len(set(names)):
            raise ValueError("gate policy dimensions must be unique")
        if len(self.required_evidence) != len(set(self.required_evidence)):
            raise ValueError("required evidence keys must be unique")
        if not set(self.required_evidence).issubset(names):
            raise ValueError(
                "required evidence keys must name policy dimensions")
        if not all(math.isfinite(value) for value in (
                self.min_overall, self.min_agreement, self.reject_max_overall)):
            raise ValueError("gate thresholds must be finite")
        if not 0.0 <= self.min_agreement <= 1.0:
            raise ValueError("min_agreement must be between 0 and 1")
        if self.reject_max_overall > self.min_overall:
            raise ValueError("reject_max_overall must not exceed min_overall")
        if any(not key.strip() for key in self.required_evidence):
            raise ValueError("required evidence keys must not be empty")
        return self

    @property
    def dimension_names(self) -> tuple[str, ...]:
        return tuple(dimension.name for dimension in self.dimensions)


def _gate(
    name: str,
    dimensions: tuple[tuple[str, ScoreDirection, Optional[float]], ...],
    *,
    required_evidence: tuple[str, ...] = (),
) -> GatePolicy:
    return GatePolicy(
        name=name,
        dimensions=tuple(
            DimensionPolicy(name=dimension, direction=direction, approve_at=approve_at)
            for dimension, direction, approve_at in dimensions
        ),
        required_evidence=required_evidence,
    )


GATE_POLICIES: Mapping[str, GatePolicy] = MappingProxyType({
    "code_edit": _gate("code_edit", (
        ("correctness", "higher", 4.0),
        ("safety", "higher", 4.5),
        ("style", "higher", 3.0),
        ("tests", "higher", 3.5),
        ("minimal_change", "higher", 4.0),
    )),
    "pr_review": _gate("pr_review", (
        ("correctness", "higher", 4.0),
        ("safety", "higher", 4.5),
        ("tests", "higher", 3.5),
        ("documentation", "higher", 3.0),
        ("scope", "higher", 4.0),
    )),
    "merge": _gate("merge", (
        ("correctness", "higher", 4.0),
        ("safety", "higher", 4.5),
        ("tests", "higher", 3.5),
        ("ci_status", "higher", 4.5),
        ("branch_protection", "higher", 4.5),
    ), required_evidence=("ci_status", "branch_protection")),
    "delegation": _gate("delegation", (
        ("feasibility", "higher", 4.0),
        ("clarity", "higher", 4.0),
        ("risk", "lower", 2.0),
        ("dependencies", "higher", 3.5),
        ("value", "higher", 3.5),
    )),
    "security": _gate("security", (
        ("vulnerability_severity", "lower", 2.0),
        ("exploitability", "lower", 2.0),
        ("fix_correctness", "higher", 4.5),
        ("blast_radius", "lower", 2.0),
        ("compliance", "higher", 4.0),
    )),
    "code_quality": _gate("code_quality", (
        ("complexity", "lower", 2.5),
        ("duplication", "lower", 2.5),
        ("test_coverage", "higher", 3.5),
        ("documentation", "higher", 3.0),
        ("maintainability", "higher", 4.0),
    )),
    "dependency": _gate("dependency", (
        ("vulnerability", "lower", 2.0),
        ("license_compliance", "higher", 4.0),
        ("version_freshness", "higher", 3.0),
        ("supply_chain", "higher", 4.0),
        ("breaking_changes", "lower", 2.5),
    )),
    "architecture": _gate("architecture", (
        ("cohesion", "higher", 4.0),
        ("coupling", "lower", 2.5),
        ("scalability", "higher", 3.5),
        ("observability", "higher", 3.5),
        ("evolution", "higher", 4.0),
    )),
})


class CouncilMember(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    id: str                       # model id, e.g. "claude-sonnet-5"
    provider: Optional[str] = None
    weight: float = 1.0
    base_url: Optional[str] = None   # for custom/OpenAI-compatible providers
    api_key: Optional[str] = Field(default=None, repr=False)  # paired with base_url

    @field_validator("id")
    @classmethod
    def _nonempty_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("member id must not be empty")
        return value

    @field_validator("weight")
    @classmethod
    def _positive(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError("weight must be finite and > 0")
        return v


class CouncilThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_overall: float = 4.0
    min_safety: float = 4.5
    min_tests: float = 3.5
    min_agreement: float = 0.7
    reject_max_overall: float = 2.5

    @model_validator(mode="after")
    def _valid_thresholds(self) -> "CouncilThresholds":
        values = (
            self.min_overall, self.min_safety, self.min_tests,
            self.min_agreement, self.reject_max_overall,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("council thresholds must be finite")
        if not 0.0 <= self.min_agreement <= 1.0:
            raise ValueError("min_agreement must be between 0 and 1")
        if self.reject_max_overall > self.min_overall:
            raise ValueError("reject_max_overall must not exceed min_overall")
        return self


class CouncilRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_type: str                       # PR | REFACTOR | DEPLOYMENT | ESCALATION | ...
    subject_ref: dict[str, Any] = Field(default_factory=dict)
    content: str                            # the diff / artifact under review
    artifact_refs: dict[str, Any] = Field(default_factory=dict)  # Engraphis ids
    evidence: dict[str, Any] = Field(default_factory=dict)
    evidence_scores: dict[str, float] = Field(default_factory=dict)
    rubric_dimensions: tuple[str, ...] = DEFAULT_DIMENSIONS
    scale_min: int = 1
    scale_max: int = 5
    decision_type: str = "REVIEW"
    risk_level: str = "medium"               # low | medium | high
    checklist: tuple[str, ...] = ()
    correlation_id: Optional[str] = None
    cache_namespace: str = "default"
    gate: Optional[str] = None
    gate_policy: Optional[GatePolicy] = None

    @field_validator("risk_level")
    @classmethod
    def _risk(cls, value: str) -> str:
        if value not in ("low", "medium", "high"):
            raise ValueError("risk_level must be low|medium|high")
        return value

    @field_validator("evidence_scores")
    @classmethod
    def _finite_evidence_scores(
        cls, scores: dict[str, float],
    ) -> dict[str, float]:
        if not all(math.isfinite(value) for value in scores.values()):
            raise ValueError("evidence_scores must be finite")
        return scores

    @field_validator("rubric_dimensions")
    @classmethod
    def _valid_dimension_names(
        cls, dimensions: tuple[str, ...],
    ) -> tuple[str, ...]:
        return tuple(
            _validate_identifier(name, "dimension name")
            for name in dimensions
        )

    @model_validator(mode="after")
    def _valid_request(self) -> "CouncilRequest":
        if self.scale_min >= self.scale_max:
            raise ValueError("scale_min must be less than scale_max")
        if not self.rubric_dimensions or len(self.rubric_dimensions) != len(
                set(self.rubric_dimensions)):
            raise ValueError("rubric_dimensions must be non-empty and unique")
        if not self.cache_namespace.strip():
            raise ValueError("cache_namespace must not be empty")
        self.subject_type = self.subject_type.strip()
        self.cache_namespace = self.cache_namespace.strip()
        if not self.subject_type:
            raise ValueError("subject_type must not be empty")
        if len(self.subject_type) > 128:
            raise ValueError("subject_type must not exceed 128 characters")
        try:
            json.dumps({
                "subject_ref": self.subject_ref,
                "artifact_refs": self.artifact_refs,
                "evidence": self.evidence,
            }, sort_keys=True, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "subject_ref, artifact_refs, and evidence must be finite JSON data"
            ) from error
        if self.gate_policy is not None:
            if self.gate is not None and self.gate != self.gate_policy.name:
                raise ValueError("gate must match gate_policy.name")
            self.gate = self.gate_policy.name
        elif self.gate is not None and self.gate not in GATE_POLICIES:
            raise ValueError(
                f"unknown gate {self.gate!r}; provide gate_policy for a custom gate")
        return self

    def effective_policy(self) -> Optional[GatePolicy]:
        if self.gate_policy is not None:
            return self.gate_policy
        return GATE_POLICIES.get(self.gate or "")

    def effective_dimensions(self) -> tuple[str, ...]:
        policy = self.effective_policy()
        return policy.dimension_names if policy is not None else self.rubric_dimensions

    def subject_hash(self) -> str:
        basis = json.dumps({
            "namespace": self.cache_namespace,
            "subject_type": self.subject_type,
            "subject_ref": self.subject_ref,
            "content": self.content,
            "artifact_refs": self.artifact_refs,
            "evidence": self.evidence,
            "evidence_scores": self.evidence_scores,
        }, sort_keys=True, ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

    def rubric_hash(self) -> str:
        policy = self.effective_policy()
        basis = json.dumps({
            "dims": list(self.effective_dimensions()),
            "scale": [self.scale_min, self.scale_max],
            "decision_type": self.decision_type,
            "risk_level": self.risk_level,
            "checklist": list(self.checklist),
            "gate": self.gate,
            "policy": policy.model_dump(mode="json") if policy is not None else None,
        }, sort_keys=True, allow_nan=False)
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()


class ModelReview(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    model_id: str = ""
    self_scores: dict[str, float]
    recommendation: str
    rationale: str = Field(default="", max_length=4000)

    @field_validator("self_scores")
    @classmethod
    def _finite_scores(cls, scores: dict[str, float]) -> dict[str, float]:
        if not all(math.isfinite(value) for value in scores.values()):
            raise ValueError("self_scores must be finite")
        return scores

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
    model_config = ConfigDict(extra="forbid")
    review_model_id: str
    overall: float
    justification: str = Field(default="", max_length=2000)

    @field_validator("overall")
    @classmethod
    def _finite_overall(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("peer overall score must be finite")
        return value


class PeerEval(BaseModel):
    model_config = ConfigDict(extra="forbid")
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


__all__ = [
    "CouncilMember", "CouncilThresholds", "CouncilRequest", "ModelReview",
    "PeerScore", "PeerEval", "CouncilDecision", "DimensionPolicy", "GatePolicy",
    "ScoreDirection", "DEFAULT_DIMENSIONS", "GATE_POLICIES", "RECOMMENDATIONS",
]
