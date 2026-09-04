"""Stable, serializable contracts shared by NAVE research workflows.

The contracts intentionally keep evidence collection and decision making
separate.  A result is a research artifact, never an execution instruction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping


class ResearchStatus(StrEnum):
    """Common terminal/intermediate statuses for research workflows."""

    SETUP_FOUND = "SETUP_FOUND"
    NO_SETUP = "NO_SETUP"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    STRATEGY_NOT_VALIDATED = "STRATEGY_NOT_VALIDATED"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    ERROR = "ERROR"


class EvidenceKind(StrEnum):
    FACT = "FACT"
    INFERENCE = "INFERENCE"
    HYPOTHESIS = "HYPOTHESIS"
    UNKNOWN = "UNKNOWN"


class SafetyBoundary(StrEnum):
    """The non-negotiable boundary carried by every research result."""

    READ_ONLY_RESEARCH_ONLY_HUMAN_GATED = "READ_ONLY_RESEARCH_ONLY_HUMAN_GATED"


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError(f"timestamp must be an ISO string, got {type(value).__name__}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include a timezone: {value!r}")
    return parsed


@dataclass(frozen=True)
class PointInTime:
    """Explicit event, availability, and decision timestamps.

    Missing ``available_at`` is represented as ``UNKNOWN`` rather than being
    guessed from ``event_time``.  This is the key distinction that prevents
    later knowledge from being presented as decision-time evidence.
    """

    event_time: datetime | None = None
    available_at: datetime | None = None
    decision_time: datetime | None = None

    @property
    def availability(self) -> str:
        if self.available_at is None or self.decision_time is None:
            return "UNKNOWN"
        return "ELIGIBLE" if self.available_at <= self.decision_time else "LATE"

    def validate(self) -> None:
        values = (self.event_time, self.available_at, self.decision_time)
        aware = [value.tzinfo is not None for value in values if value is not None]
        if aware and not all(aware):
            raise ValueError("all supplied timestamps must either be timezone-aware or absent")
        if self.available_at and self.decision_time and self.available_at > self.decision_time:
            # This is valid as a recorded late source, but callers must not use it
            # as eligible evidence.  It is deliberately not silently corrected.
            return

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_time": _iso(self.event_time),
            "available_at": _iso(self.available_at),
            "decision_time": _iso(self.decision_time),
            "availability": self.availability,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "PointInTime":
        value = value or {}
        return cls(
            event_time=_parse_datetime(value.get("event_time")),
            available_at=_parse_datetime(value.get("available_at")),
            decision_time=_parse_datetime(value.get("decision_time")),
        )


@dataclass(frozen=True)
class EvidenceReference:
    """A source-backed claim with an explicit point-in-time record."""

    reference_id: str
    source: str
    claim: str
    kind: EvidenceKind = EvidenceKind.FACT
    confidence: float | None = None
    point_in_time: PointInTime = field(default_factory=PointInTime)
    citation: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.reference_id.strip():
            raise ValueError("evidence reference_id is required")
        if not self.source.strip():
            raise ValueError("evidence source is required")
        if not self.claim.strip():
            raise ValueError("evidence claim is required")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("evidence confidence must be between 0 and 1")
        self.point_in_time.validate()

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "source": self.source,
            "claim": self.claim,
            "kind": self.kind.value,
            "confidence": self.confidence,
            "point_in_time": self.point_in_time.to_dict(),
            "citation": self.citation,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvidenceReference":
        return cls(
            reference_id=str(value.get("reference_id") or ""),
            source=str(value.get("source") or ""),
            claim=str(value.get("claim") or ""),
            kind=EvidenceKind(str(value.get("kind") or EvidenceKind.FACT.value)),
            confidence=value.get("confidence"),
            point_in_time=PointInTime.from_dict(value.get("point_in_time")),
            citation=value.get("citation"),
            metadata=value.get("metadata") or {},
        )


@dataclass(frozen=True)
class RunMetadata:
    """Identity and timing for one deterministic or model-assisted run."""

    strategy_name: str
    strategy_version: str
    run_id: str
    decision_time: datetime
    started_at: datetime
    completed_at: datetime | None = None
    input_available_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.strategy_name.strip() or not self.strategy_version.strip() or not self.run_id.strip():
            raise ValueError("strategy_name, strategy_version, and run_id are required")
        PointInTime(
            decision_time=self.decision_time,
            available_at=self.input_available_at,
        ).validate()

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "run_id": self.run_id,
            "decision_time": _iso(self.decision_time),
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
            "input_available_at": _iso(self.input_available_at),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunMetadata":
        decision_time = _parse_datetime(value.get("decision_time"))
        started_at = _parse_datetime(value.get("started_at"))
        if decision_time is None or started_at is None:
            raise ValueError("run metadata requires decision_time and started_at")
        return cls(
            strategy_name=str(value.get("strategy_name") or ""),
            strategy_version=str(value.get("strategy_version") or ""),
            run_id=str(value.get("run_id") or ""),
            decision_time=decision_time,
            started_at=started_at,
            completed_at=_parse_datetime(value.get("completed_at")),
            input_available_at=_parse_datetime(value.get("input_available_at")),
        )


@dataclass(frozen=True)
class ResearchResult:
    """Structured output contract consumed by Quant and later evaluators."""

    workflow: str
    status: ResearchStatus
    metadata: RunMetadata
    payload: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[EvidenceReference, ...] = ()
    warnings: tuple[str, ...] = ()
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    safety_boundary: SafetyBoundary = SafetyBoundary.READ_ONLY_RESEARCH_ONLY_HUMAN_GATED

    def validate(self) -> None:
        if not self.workflow.strip():
            raise ValueError("workflow is required")
        if not isinstance(self.status, ResearchStatus):
            raise ValueError("status must be a ResearchStatus")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be a mapping")
        if self.metadata.decision_time.tzinfo is None:
            raise ValueError("decision_time must include a timezone")
        if self.status is ResearchStatus.SETUP_FOUND and not self.evidence:
            raise ValueError("SETUP_FOUND requires at least one evidence reference")
        if self.status is ResearchStatus.ERROR and not self.warnings:
            raise ValueError("ERROR requires a visible warning")
        for reference in self.evidence:
            reference.point_in_time.validate()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": 1,
            "workflow": self.workflow,
            "status": self.status.value,
            "metadata": self.metadata.to_dict(),
            "generated_at": _iso(self.generated_at),
            "point_in_time": {"decision_time": _iso(self.metadata.decision_time)},
            "payload": dict(self.payload),
            "evidence": [item.to_dict() for item in self.evidence],
            "warnings": list(self.warnings),
            "safety_boundary": self.safety_boundary.value,
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False, default=str)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ResearchResult":
        metadata = RunMetadata.from_dict(value.get("metadata") or {})
        generated_at = _parse_datetime(value.get("generated_at")) or datetime.now(UTC)
        result = cls(
            workflow=str(value.get("workflow") or ""),
            status=ResearchStatus(str(value.get("status") or "")),
            metadata=metadata,
            payload=value.get("payload") or {},
            evidence=tuple(EvidenceReference.from_dict(item) for item in value.get("evidence") or []),
            warnings=tuple(str(item) for item in value.get("warnings") or []),
            generated_at=generated_at,
            safety_boundary=SafetyBoundary(
                str(value.get("safety_boundary") or SafetyBoundary.READ_ONLY_RESEARCH_ONLY_HUMAN_GATED)
            ),
        )
        result.validate()
        return result

    def to_markdown(self) -> str:
        self.validate()
        lines = [
            f"# {self.workflow}",
            "",
            f"- Status: **{self.status.value}**",
            f"- Strategy: `{self.metadata.strategy_name}` v`{self.metadata.strategy_version}`",
            f"- Run: `{self.metadata.run_id}`",
            f"- Decision time: `{_iso(self.metadata.decision_time)}`",
            f"- Safety: `{self.safety_boundary.value}`",
            "",
            "## Result",
            "",
        ]
        if self.payload:
            for key, value in self.payload.items():
                lines.append(f"- **{key}:** {value}")
        else:
            lines.append("_No structured payload._")
        lines.extend(["", "## Evidence", ""])
        if self.evidence:
            for item in self.evidence:
                availability = item.point_in_time.availability
                citation = f" — {item.citation}" if item.citation else ""
                lines.append(
                    f"- `{item.kind.value}` `{item.reference_id}` ({availability}): "
                    f"{item.claim} [{item.source}]{citation}"
                )
        else:
            lines.append("_No evidence references._")
        if self.warnings:
            lines.extend(["", "## Warnings", ""])
            lines.extend(f"- {warning}" for warning in self.warnings)
        return "\n".join(lines) + "\n"
