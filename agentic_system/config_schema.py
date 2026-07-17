"""Configuration validation with Pydantic schemas."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CouncilMemberConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    id: str
    provider: Optional[str] = None
    weight: float = 1.0
    base_url: Optional[str] = None
    api_key: Optional[str] = None

    @field_validator("weight")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("weight must be > 0")
        return v


class CouncilThresholdsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_overall: float = 4.0
    min_safety: float = 4.5
    min_tests: float = 3.5
    min_agreement: float = 0.7
    reject_max_overall: float = 2.5


class CouncilConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    members: list[CouncilMemberConfig] = Field(default_factory=list)
    thresholds: CouncilThresholdsConfig = Field(default_factory=CouncilThresholdsConfig)
    peer_eval: str = "high_risk_only"  # never | high_risk_only | always
    min_quorum: int = 2

    @field_validator("peer_eval")
    @classmethod
    def _peer(cls, v: str) -> str:
        if v not in ("never", "high_risk_only", "always"):
            raise ValueError("peer_eval must be never|high_risk_only|always")
        return v


class BreakerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    global_auto_recover_seconds: int = 300
    agent_auto_close_on_success: bool = True
    workflow_auto_close_on_success: bool = True


class NoProgressConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window: int = 3
    threshold: float = 0.9
    embedding_backend: str = "auto"  # auto | sentence-transformers | deterministic | engraphis
    embedding_model: str = "all-MiniLM-L6-v2"


class SweepsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    heartbeat_enabled: bool = True
    heartbeat_schedule: str = "*/5 * * * *"
    stuck_recovery_enabled: bool = True
    stuck_recovery_schedule: str = "*/10 * * * *"
    stuck_threshold_min: int = 30
    watchdog_enabled: bool = True
    watchdog_schedule: str = "*/5 * * * *"
    consolidate_enabled: bool = True
    consolidate_schedule: str = "0 3 * * *"
    event_retention_days: int = 30


class OrchestrationConfig(BaseModel):
    """Top-level orchestration configuration."""
    model_config = ConfigDict(extra="forbid")

    orchestration_enabled: bool = True
    events_db_path: str = str(Path.home() / ".agentic" / "events.db")

    council: CouncilConfig = Field(default_factory=CouncilConfig)
    breakers: BreakerConfig = Field(default_factory=BreakerConfig)
    no_progress: NoProgressConfig = Field(default_factory=NoProgressConfig)
    sweeps: SweepsConfig = Field(default_factory=SweepsConfig)

    # Host-specific overrides (populated by ConfigPort)
    state_tool_policy: Optional[dict] = None
    high_impact_tool_patterns: Optional[tuple[str, ...]] = None


def load_config(path: str) -> OrchestrationConfig:
    """Load and validate config from YAML file."""
    import yaml
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return OrchestrationConfig(**data)


def validate_config(config: OrchestrationConfig) -> list[str]:
    """Return list of warnings (non-fatal issues)."""
    warnings = []
    if not config.council.members:
        warnings.append("council.members is empty - council reviews will fail")
    if config.council.min_quorum > len(config.council.members):
        warnings.append(f"council.min_quorum ({config.council.min_quorum}) > members ({len(config.council.members)})")
    if config.breakers.global_auto_recover_seconds < 60:
        warnings.append("breakers.global_auto_recover_seconds < 60 may cause flapping")
    return warnings


__all__ = [
    "OrchestrationConfig", "CouncilConfig", "CouncilMemberConfig",
    "CouncilThresholdsConfig", "BreakerConfig", "NoProgressConfig",
    "SweepsConfig", "load_config", "validate_config",
]