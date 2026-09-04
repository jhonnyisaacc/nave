"""Read-only options research contracts and lifecycle.

This module deliberately stops at research.  It normalizes the inputs that a
future options strategy can use, records what was available at decision time,
and evaluates supplied outcomes.  It does not price an order, size a trade, or
call a broker.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
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


class OptionDomain(StrEnum):
    CRYPTO = "crypto"
    STOCKS = "stocks"


class StrategyState(StrEnum):
    EXPERIMENTAL = "EXPERIMENTAL"
    PROMISING = "PROMISING"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class OptionStrategyDefinition:
    """A named research strategy, not an execution recipe."""

    name: str
    domain: OptionDomain
    underlyings: tuple[str, ...]
    description: str
    defined_risk_required: bool = True
    state: StrategyState = StrategyState.EXPERIMENTAL
    version: str = "1.0.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "domain": self.domain.value,
            "underlyings": list(self.underlyings),
            "description": self.description,
            "defined_risk_required": self.defined_risk_required,
            "state": self.state.value,
            "version": self.version,
            "read_only": True,
        }


def _number(value: object) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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


@dataclass(frozen=True)
class VolatilityInputs:
    """Point-in-time volatility and thesis inputs for one underlying."""

    underlying: str
    event_time: datetime | None
    available_at: datetime | None
    decision_time: datetime
    macro_regime: str | None = None
    volatility_regime: str | None = None
    implied_volatility: float | None = None
    realized_volatility: float | None = None
    skew: float | None = None
    term_structure: str | None = None
    catalyst: str | None = None
    directional_thesis: str | None = None
    defined_risk: bool = False
    source: str = "provided_snapshot"
    source_url: str | None = None

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any], *, decision_time: datetime) -> "VolatilityInputs":
        underlying = str(row.get("underlying") or row.get("asset") or "").strip().upper()
        if not underlying:
            raise ValueError("underlying is required")
        event_time = _timestamp(row.get("event_time"), field="event_time")
        available_at = _timestamp(row.get("available_at"), field="available_at")
        return cls(
            underlying=underlying,
            event_time=event_time,
            available_at=available_at,
            decision_time=decision_time,
            macro_regime=str(row["macro_regime"]) if row.get("macro_regime") not in (None, "") else None,
            volatility_regime=(
                str(row["volatility_regime"])
                if row.get("volatility_regime") not in (None, "")
                else None
            ),
            implied_volatility=_number(row.get("implied_volatility")),
            realized_volatility=_number(row.get("realized_volatility")),
            skew=_number(row.get("skew")),
            term_structure=str(row["term_structure"]) if row.get("term_structure") not in (None, "") else None,
            catalyst=str(row["catalyst"]) if row.get("catalyst") not in (None, "") else None,
            directional_thesis=(
                str(row["directional_thesis"])
                if row.get("directional_thesis") not in (None, "")
                else None
            ),
            defined_risk=bool(row.get("defined_risk", False)),
            source=str(row.get("source") or "provided_snapshot"),
            source_url=str(row["source_url"]) if row.get("source_url") else None,
        )

    @property
    def point_in_time(self) -> PointInTime:
        return PointInTime(
            event_time=self.event_time,
            available_at=self.available_at,
            decision_time=self.decision_time,
        )

    @property
    def iv_rv_spread(self) -> float | None:
        if self.implied_volatility is None or self.realized_volatility is None:
            return None
        return self.implied_volatility - self.realized_volatility

    def missing_dimensions(self) -> list[str]:
        missing: list[str] = []
        if self.implied_volatility is None:
            missing.append("implied_volatility")
        if self.realized_volatility is None:
            missing.append("realized_volatility")
        if not self.defined_risk:
            missing.append("defined_risk")
        return missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "point_in_time": self.point_in_time.to_dict(),
            "macro_regime": self.macro_regime,
            "volatility_regime": self.volatility_regime,
            "implied_volatility": self.implied_volatility,
            "realized_volatility": self.realized_volatility,
            "iv_rv_spread": self.iv_rv_spread,
            "skew": self.skew,
            "term_structure": self.term_structure,
            "catalyst": self.catalyst,
            "directional_thesis": self.directional_thesis,
            "defined_risk": self.defined_risk,
            "source": self.source,
            "source_url": self.source_url,
        }


def strategy_definition(domain: OptionDomain | str, name: str | None = None) -> OptionStrategyDefinition:
    domain = OptionDomain(domain)
    if domain is OptionDomain.CRYPTO:
        return OptionStrategyDefinition(
            name=name or "crypto_iv_rv_defined_risk",
            domain=domain,
            underlyings=("BTC", "ETH"),
            description="Compare implied and realized volatility with macro, catalyst, and defined-risk context.",
        )
    return OptionStrategyDefinition(
        name=name or "stocks_volatility_catalyst_defined_risk",
        domain=domain,
        underlyings=("EQUITY_UNIVERSE",),
        description="Screen equity options volatility, catalysts, and directional thesis with defined risk.",
    )


class OptionResearchWorkflow:
    """Reusable options scan/evaluation lifecycle with conservative states."""

    def __init__(self, *, store: ResearchStore | None = None, clock: Any = None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))

    def _decision_time(self, value: datetime | str | None) -> datetime:
        parsed = _timestamp(value, field="decision_time") if value is not None else self.clock()
        if parsed is None:
            raise ValueError("decision_time is required")
        return parsed

    @staticmethod
    def _run_id(workflow: str, decision_time: datetime, count: int) -> str:
        raw = f"{workflow}|{decision_time.astimezone(UTC).isoformat()}|{count}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _metadata(self, definition: OptionStrategyDefinition, phase: str, decision_time: datetime, count: int) -> RunMetadata:
        workflow = f"options.{definition.domain.value}.{phase}"
        return RunMetadata(
            strategy_name=definition.name,
            strategy_version=definition.version,
            run_id=self._run_id(workflow, decision_time, count),
            decision_time=decision_time,
            started_at=decision_time,
            completed_at=decision_time,
        )

    @staticmethod
    def _evidence(item: VolatilityInputs, index: int) -> EvidenceReference:
        return EvidenceReference(
            reference_id=f"option-input-{index + 1}",
            source=item.source,
            claim=f"{item.underlying} options snapshot supplied for research evaluation",
            kind=EvidenceKind.FACT,
            point_in_time=item.point_in_time,
            citation=item.source_url,
        )

    def scan(
        self,
        domain: OptionDomain | str,
        rows: Sequence[Mapping[str, Any]],
        *,
        decision_time: datetime | str | None = None,
        persist: bool = False,
    ) -> ResearchResult:
        definition = strategy_definition(domain)
        decided = self._decision_time(decision_time)
        normalized: list[VolatilityInputs] = []
        rejected: list[dict[str, Any]] = []
        warnings: list[str] = []
        for index, row in enumerate(rows):
            try:
                item = VolatilityInputs.from_mapping(row, decision_time=decided)
            except ValueError as exc:
                rejected.append({"row": index, "reason": "invalid_input", "detail": str(exc)})
                continue
            if item.point_in_time.availability != "ELIGIBLE":
                reason = "availability_unknown" if item.available_at is None else "available_after_decision_time"
                rejected.append({"asset": item.underlying, "reason": reason})
                continue
            if definition.domain is OptionDomain.CRYPTO and item.underlying not in definition.underlyings:
                rejected.append({"asset": item.underlying, "reason": "outside_crypto_scope"})
                continue
            missing = item.missing_dimensions()
            if missing:
                rejected.append({"asset": item.underlying, "reason": "missing_dimensions", "dimensions": missing})
                continue
            normalized.append(item)

        status = ResearchStatus.STRATEGY_NOT_VALIDATED if normalized else ResearchStatus.INSUFFICIENT_EVIDENCE
        if not rows:
            warnings.append("No point-in-time options snapshots were supplied; no recommendation was made.")
        elif not normalized:
            warnings.append("No snapshot had sufficient point-in-time volatility and defined-risk inputs.")
        else:
            warnings.append("The options strategy is not validated; observations are research-only.")
        result = ResearchResult(
            workflow=f"options.{definition.domain.value}.scan",
            status=status,
            metadata=self._metadata(definition, "scan", decided, len(rows)),
            payload={
                "strategy": definition.to_dict(),
                "strategy_state": definition.state.value,
                "universe": list(definition.underlyings),
                "eligible_inputs": len(normalized),
                "rejected_inputs": rejected,
                "observations": [item.to_dict() for item in normalized],
                "research_only": True,
                "execution_enabled": False,
            },
            evidence=tuple(self._evidence(item, index) for index, item in enumerate(normalized)),
            warnings=tuple(warnings),
            safety_boundary=SafetyBoundary.READ_ONLY_RESEARCH_ONLY_HUMAN_GATED,
        )
        result.validate()
        if persist and self.store:
            self.store.save_result(result)
        return result

    def evaluate(
        self,
        domain: OptionDomain | str,
        outcomes: Sequence[Mapping[str, Any]],
        *,
        strategy_name: str | None = None,
        decision_time: datetime | str | None = None,
        persist: bool = False,
    ) -> ResearchResult:
        definition = strategy_definition(domain, strategy_name)
        decided = self._decision_time(decision_time)
        returns = [_number(row.get("forward_return_pct", row.get("return_pct"))) for row in outcomes]
        valid_returns = [value for value in returns if value is not None]
        cumulative = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for value in valid_returns:
            cumulative += value
            peak = max(peak, cumulative)
            max_drawdown = min(max_drawdown, cumulative - peak)
        sample_size = len(valid_returns)
        average_return = sum(valid_returns) / sample_size if sample_size else None
        win_rate = sum(value > 0 for value in valid_returns) / sample_size if sample_size else None
        if sample_size < 30:
            state = StrategyState.EXPERIMENTAL
        elif average_return is not None and average_return > 0:
            state = StrategyState.PROMISING
        else:
            state = StrategyState.REJECTED
        status = ResearchStatus.STRATEGY_NOT_VALIDATED if sample_size else ResearchStatus.INSUFFICIENT_EVIDENCE
        result = ResearchResult(
            workflow=f"options.{definition.domain.value}.evaluate",
            status=status,
            metadata=self._metadata(definition, "evaluate", decided, len(outcomes)),
            payload={
                "strategy": {**definition.to_dict(), "state": state.value},
                "strategy_state": state.value,
                "metrics": {
                    "sample_size": sample_size,
                    "average_forward_return_pct": average_return,
                    "win_rate": win_rate,
                    "max_drawdown_pct": max_drawdown if sample_size else None,
                },
                "validation_gate": "No automatic VALIDATED state; require an explicit review of sample, costs, and regime stability.",
                "execution_enabled": False,
            },
            warnings=(
                "Insufficient outcome history to evaluate the strategy." if not sample_size else
                "Evaluation is research-only; VALIDATED requires explicit human review."
            ,),
            safety_boundary=SafetyBoundary.READ_ONLY_RESEARCH_ONLY_HUMAN_GATED,
        )
        result.validate()
        if persist and self.store:
            self.store.save_result(result)
        return result

