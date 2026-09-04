"""Multi-factor, human-gated stock-short research.

The scanner is intentionally a research layer.  A bearish macro regime is
never sufficient by itself, and rejected candidates remain in the result so
the strategy can be audited for systematic blind spots.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from research.core.contracts import (
    EvidenceKind,
    EvidenceReference,
    PointInTime,
    ResearchResult,
    ResearchStatus,
    RunMetadata,
    SafetyBoundary,
)
from research.core.store import ResearchStore


_FACTORS = (
    "macro_regime",
    "sector_weakness",
    "earnings_revision_deterioration",
    "valuation_support",
    "technical_breakdown",
    "catalyst",
    "positioning_crowding",
)


def _timestamp(value: object, *, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO timestamp") from exc
    else:
        raise ValueError(f"{field} must be an ISO timestamp")
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def _flag(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "bearish", "weak", "high"}
    return bool(value)


def _decision_time(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    parsed = _timestamp(value, field="decision_time")
    if parsed is None:
        raise ValueError("decision_time is required")
    return parsed


def _run_id(phase: str, decision_time: datetime, count: int) -> str:
    raw = f"stocks.short.{phase}|{decision_time.astimezone(UTC).isoformat()}|{count}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _metadata(phase: str, decision_time: datetime, count: int) -> RunMetadata:
    return RunMetadata(
        strategy_name="stock_short_multifactor",
        strategy_version="1.0.0",
        run_id=_run_id(phase, decision_time, count),
        decision_time=decision_time,
        started_at=decision_time,
        completed_at=decision_time,
    )


def _evidence(row: Mapping[str, Any], index: int, decision_time: datetime) -> EvidenceReference:
    available_at = _timestamp(row.get("available_at"), field="available_at")
    event_time = _timestamp(row.get("event_time"), field="event_time")
    ticker = str(row.get("ticker") or row.get("asset") or "UNKNOWN").upper()
    return EvidenceReference(
        reference_id=f"short-input-{index + 1}",
        source=str(row.get("source") or "provided_snapshot"),
        claim=f"{ticker} multi-factor short research snapshot",
        kind=EvidenceKind.FACT,
        point_in_time=PointInTime(
            event_time=event_time,
            available_at=available_at,
            decision_time=decision_time,
        ),
        citation=str(row["source_url"]) if row.get("source_url") else None,
    )


class StockShortResearchWorkflow:
    """Scan and evaluate stock-short research snapshots without execution."""

    def __init__(self, *, store: ResearchStore | None = None):
        self.store = store

    @staticmethod
    def _candidate(row: Mapping[str, Any]) -> dict[str, Any]:
        ticker = str(row.get("ticker") or row.get("asset") or "").strip().upper()
        macro = str(row.get("macro_regime") or "").strip().lower()
        factors = {
            "macro_regime": macro or None,
            "sector_weakness": _flag(row.get("sector_weakness")),
            "earnings_revision_deterioration": _flag(row.get("earnings_revision_deterioration")),
            "valuation_support": _flag(row.get("valuation_support")),
            "technical_breakdown": _flag(row.get("technical_breakdown")),
            "catalyst": str(row.get("catalyst") or "").strip() or None,
            "positioning_crowding": _flag(row.get("positioning_crowding")),
        }
        non_macro = sum(
            bool(factors[name]) for name in _FACTORS if name != "macro_regime"
        )
        total = non_macro + int(macro in {"bearish", "risk_off", "contraction"})
        passed = [name for name in _FACTORS if factors[name]]
        if total < 3:
            reason = "macro_only" if macro in {"bearish", "risk_off", "contraction"} and non_macro < 2 else "insufficient_factors"
            selected = False
        elif non_macro < 2:
            reason = "macro_only"
            selected = False
        else:
            reason = None
            selected = True
        return {
            "asset": ticker,
            "direction": "short",
            "thesis": str(row.get("thesis") or "Multi-factor downside research candidate"),
            "factors": factors,
            "filters_passed": passed,
            "factor_count": total,
            "entry_research_zone": row.get("entry_research_zone"),
            "invalidation": row.get("invalidation"),
            "horizon": row.get("horizon") or "research-defined",
            "confidence": row.get("confidence"),
            "major_risks": list(row.get("major_risks") or []),
            "selected": selected,
            "rejection_reason": reason,
            "research_only": True,
        }

    def scan(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        decision_time: datetime | str | None = None,
        persist: bool = False,
    ) -> ResearchResult:
        decided = _decision_time(decision_time)
        candidates: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        evidence: list[EvidenceReference] = []
        for index, row in enumerate(rows):
            ticker = str(row.get("ticker") or row.get("asset") or "").strip().upper()
            try:
                available_at = _timestamp(row.get("available_at"), field="available_at")
                if not ticker:
                    raise ValueError("ticker is required")
                if available_at is None:
                    rejected.append({"asset": ticker or "UNKNOWN", "reason": "availability_unknown"})
                    continue
                if available_at > decided:
                    rejected.append({"asset": ticker, "reason": "available_after_decision_time"})
                    continue
                candidate = self._candidate(row)
                evidence.append(_evidence(row, index, decided))
            except ValueError as exc:
                rejected.append({"asset": ticker or "UNKNOWN", "reason": "invalid_input", "detail": str(exc)})
                continue
            if candidate["selected"]:
                candidates.append(candidate)
            else:
                rejected.append({**candidate, "reason": candidate["rejection_reason"]})

        if candidates:
            status = ResearchStatus.SETUP_FOUND
        elif rows and evidence:
            status = ResearchStatus.NO_SETUP
        else:
            status = ResearchStatus.INSUFFICIENT_EVIDENCE
        warnings = (
            ("No point-in-time stock snapshots were supplied; no short research candidate was produced.",)
            if not rows else
            ("No multi-factor short setup passed; rejected candidates are retained for audit.",)
            if not candidates else
            ("Research candidate only; a bearish macro view alone is never sufficient and no execution is enabled.",)
        )
        result = ResearchResult(
            workflow="stocks.short.scan",
            status=status,
            metadata=_metadata("scan", decided, len(rows)),
            payload={
                "universe_scanned": len(rows),
                "final_candidates": candidates,
                "rejected_candidates": rejected,
                "factor_definition": list(_FACTORS),
                "execution_enabled": False,
                "human_decision_required": True,
            },
            evidence=tuple(evidence),
            warnings=warnings,
            safety_boundary=SafetyBoundary.READ_ONLY_RESEARCH_ONLY_HUMAN_GATED,
        )
        result.validate()
        if persist and self.store:
            self.store.save_result(result)
        return result

    def evaluate(
        self,
        outcomes: Sequence[Mapping[str, Any]],
        *,
        decision_time: datetime | str | None = None,
        persist: bool = False,
    ) -> ResearchResult:
        decided = _decision_time(decision_time)
        returns = []
        selected: list[str] = []
        rejected: list[str] = []
        for row in outcomes:
            value = row.get("short_return_pct", row.get("forward_return_pct"))
            try:
                outcome = float(value)
            except (TypeError, ValueError):
                continue
            returns.append(outcome)
            asset = str(row.get("asset") or row.get("ticker") or "UNKNOWN").upper()
            (selected if bool(row.get("selected", True)) else rejected).append(asset)
        sample_size = len(returns)
        result = ResearchResult(
            workflow="stocks.short.evaluate",
            status=ResearchStatus.STRATEGY_NOT_VALIDATED if sample_size else ResearchStatus.INSUFFICIENT_EVIDENCE,
            metadata=_metadata("evaluate", decided, len(outcomes)),
            payload={
                "selected_assets": selected,
                "rejected_assets": rejected,
                "metrics": {
                    "sample_size": sample_size,
                    "mean_short_return_pct": sum(returns) / sample_size if sample_size else None,
                    "hit_rate": sum(value > 0 for value in returns) / sample_size if sample_size else None,
                },
                "execution_enabled": False,
            },
            warnings=("Evaluation is research-only; no automatic short action is permitted.",),
            safety_boundary=SafetyBoundary.READ_ONLY_RESEARCH_ONLY_HUMAN_GATED,
        )
        result.validate()
        if persist and self.store:
            self.store.save_result(result)
        return result

    def missed_moves(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        decision_time: datetime | str | None = None,
        threshold_pct: float = 20.0,
        persist: bool = False,
    ) -> ResearchResult:
        decided = _decision_time(decision_time)
        missed: list[dict[str, Any]] = []
        unknown: list[dict[str, Any]] = []
        for row in rows:
            asset = str(row.get("asset") or row.get("ticker") or "UNKNOWN").upper()
            available_at = _timestamp(row.get("available_at"), field="available_at")
            later_move = row.get("later_move_pct")
            try:
                move = float(later_move)
            except (TypeError, ValueError):
                continue
            if available_at is None or available_at > decided:
                unknown.append({"asset": asset, "reason": "decision_time_information_unknown"})
                continue
            if abs(move) < threshold_pct or bool(row.get("selected", False)):
                continue
            missed.append({
                "asset": asset,
                "decision_time": decided.isoformat(),
                "later_move_pct": move,
                "information_before_move": row.get("information_before"),
                "universe_member": row.get("universe_member"),
                "failed_filter": row.get("failed_filter"),
                "possible_missing_feature": row.get("possible_missing_feature"),
            })
        status = ResearchStatus.ACTION_REQUIRED if missed else ResearchStatus.NO_SETUP
        result = ResearchResult(
            workflow="stocks.short.missed_moves",
            status=status,
            metadata=_metadata("missed-moves", decided, len(rows)),
            payload={
                "threshold_pct": threshold_pct,
                "missed_moves": missed,
                "information_unknown": unknown,
                "hindsight_data_used_as_signal": False,
                "execution_enabled": False,
            },
            warnings=(
                ("Missed moves require human review for systematic blind spots.",) if missed else
                ("No eligible missed move exceeded the review threshold.",)
            ),
            safety_boundary=SafetyBoundary.READ_ONLY_RESEARCH_ONLY_HUMAN_GATED,
        )
        result.validate()
        if persist and self.store:
            self.store.save_result(result)
        return result

