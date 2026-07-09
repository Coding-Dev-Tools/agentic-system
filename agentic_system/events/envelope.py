"""Canonical event envelope for the Hermes orchestration layer.

Single envelope for all control-flow events (task lifecycle, workflow
transitions, agent state changes, council decisions, breaker trips).
Adopted verbatim from HERMES_ENGRAPHIS_AGENTIC_UPGRADE_HANDOFF.md §3.2.

Design rules:
- Validated with pydantic on both publish and consume; malformed events are
  rejected at the boundary (hallucination guard).
- Events carry *references* into Engraphis (namespace + memory id) for large
  payloads — never the payloads themselves.
- ``correlation_id`` groups everything belonging to one workflow run / task
  chain; ``causation_id`` points at the event that directly caused this one.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "1.0"

_PRIORITIES = ("low", "normal", "high", "critical")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_event_id() -> str:
    return f"evt-{uuid.uuid4()}"


class EventEnvelope(BaseModel):
    """One event. Append-only; never mutated after publish."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_event_id)
    type: str
    source: str = "hermes"
    target: Optional[str] = None
    aggregate_type: Optional[str] = None
    aggregate_id: Optional[str] = None
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None
    created_at: str = Field(default_factory=_now_iso)
    schema_version: str = SCHEMA_VERSION
    payload: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: Optional[int] = None
    priority: str = "normal"

    @field_validator("type")
    @classmethod
    def _type_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("event type must be a non-empty string")
        return v.strip()

    @field_validator("priority")
    @classmethod
    def _priority_known(cls, v: str) -> str:
        if v not in _PRIORITIES:
            raise ValueError(f"priority must be one of {_PRIORITIES}, got {v!r}")
        return v

    @field_validator("ttl_seconds")
    @classmethod
    def _ttl_positive(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v <= 0:
            raise ValueError("ttl_seconds must be positive when set")
        return v

    def caused_by(self, parent: "EventEnvelope") -> "EventEnvelope":
        """Return a copy linked to ``parent`` (same correlation, new causation)."""
        return self.model_copy(
            update={
                "causation_id": parent.id,
                "correlation_id": self.correlation_id or parent.correlation_id,
            }
        )


__all__ = ["EventEnvelope", "SCHEMA_VERSION", "new_event_id"]
