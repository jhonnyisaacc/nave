"""Thin Quant presentation and safe job-declaration helpers."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research.core.contracts import ResearchResult, ResearchStatus


@dataclass(frozen=True)
class JobDeclaration:
    key: str
    command: str
    schedule: str
    timezone: str
    old_job: str | None
    enabled: bool
    production_ready: bool
    migration_status: str
    model_lanes: Mapping[str, tuple[str, ...]]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "JobDeclaration":
        required = ("key", "command", "schedule", "timezone", "migration_status")
        missing = [name for name in required if not str(value.get(name) or "").strip()]
        if missing:
            raise ValueError(f"job declaration missing: {', '.join(missing)}")
        raw_lanes = value.get("model_lanes") or {}
        if not isinstance(raw_lanes, Mapping):
            raise ValueError("model_lanes must be an object")
        return cls(
            key=str(value["key"]),
            command=str(value["command"]),
            schedule=str(value["schedule"]),
            timezone=str(value["timezone"]),
            old_job=str(value["old_job"]) if value.get("old_job") else None,
            enabled=bool(value.get("enabled", False)),
            production_ready=bool(value.get("production_ready", False)),
            migration_status=str(value["migration_status"]),
            model_lanes={
                str(name): tuple(str(item) for item in items)
                for name, items in raw_lanes.items()
                if isinstance(items, Sequence) and not isinstance(items, (str, bytes))
            },
        )


def load_job_declarations(path: Path) -> list[JobDeclaration]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("jobs") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list):
        raise ValueError("job declaration file must contain a list or jobs array")
    return [JobDeclaration.from_mapping(row) for row in rows]


def duplicate_job_keys(declarations: Sequence[JobDeclaration]) -> list[str]:
    counts = Counter(item.key for item in declarations)
    return sorted(key for key, count in counts.items() if count > 1)


def validate_job_declarations(declarations: Sequence[JobDeclaration]) -> list[str]:
    errors = [f"duplicate key: {key}" for key in duplicate_job_keys(declarations)]
    for item in declarations:
        if item.enabled and not item.production_ready:
            errors.append(f"enabled job is not production-ready: {item.key}")
        if item.migration_status != "PREPARE_ONLY" and not item.enabled:
            errors.append(f"disabled job must be PREPARE_ONLY: {item.key}")
    return errors


def present_result(result: ResearchResult | Mapping[str, Any]) -> dict[str, Any]:
    """Return the concise, evidence-aware object Quant can present."""
    if not isinstance(result, ResearchResult):
        result = ResearchResult.from_dict(result)
    result.validate()
    payload = dict(result.payload)
    if result.status is ResearchStatus.NO_SETUP:
        next_action = "Present NO_SETUP with scan evidence; do not invent a strategy thesis."
    elif result.status is ResearchStatus.ACTION_REQUIRED:
        next_action = "Present ACTION_REQUIRED and wait for human review."
    elif result.status in {ResearchStatus.INSUFFICIENT_EVIDENCE, ResearchStatus.DATA_UNAVAILABLE}:
        next_action = "Present the evidence gap and do not recommend an action."
    else:
        next_action = "Present the structured research result and preserve its evidence and warnings."
    return {
        "workflow": result.workflow,
        "status": result.status.value,
        "strategy": result.metadata.strategy_name,
        "strategy_version": result.metadata.strategy_version,
        "decision_time": result.metadata.decision_time.isoformat(),
        "evidence_count": len(result.evidence),
        "warnings": list(result.warnings),
        "summary": {
            "candidate_count": len(payload.get("final_candidates") or payload.get("candidates") or []),
            "rejected_count": len(payload.get("rejected_candidates") or []),
            "execution_enabled": bool(payload.get("execution_enabled", False)),
        },
        "next_action": next_action,
        "human_decision_required": True,
        "safety_boundary": result.safety_boundary.value,
    }

