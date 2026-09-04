"""Human-gated portfolio research workflow.

This is a thin NAVE adapter around the existing ISM equity funnel. Portfolio
state is deliberately user-local; the public repository contains only the
schema and deterministic evaluation rules.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from research.core.contracts import EvidenceKind, EvidenceReference, PointInTime, ResearchResult, ResearchStatus, RunMetadata
from research.core.store import ResearchStore
from trading.stocks.ism_equity_pipeline import build_ism_equity_pipeline


class PortfolioAction:
    ADD_CANDIDATE = "ADD_CANDIDATE"
    HOLD = "HOLD"
    REDUCE_CANDIDATE = "REDUCE_CANDIDATE"
    EXIT_CANDIDATE = "EXIT_CANDIDATE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True)
class PositionState:
    ticker: str
    position_status: str = "open"
    approximate_entry: float | None = None
    thesis: str = ""
    source_strategy: str = "manual"
    watch_target: float | None = None
    candidate_status: str = "current"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"ticker": self.ticker.upper()}


@dataclass(frozen=True)
class PortfolioState:
    positions: tuple[PositionState, ...] = ()
    watchlist: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PortfolioState":
        positions = tuple(
            PositionState(
                ticker=str(item.get("ticker") or "").upper(),
                position_status=str(item.get("position_status") or "open"),
                approximate_entry=item.get("approximate_entry"),
                thesis=str(item.get("thesis") or ""),
                source_strategy=str(item.get("source_strategy") or "manual"),
                watch_target=item.get("watch_target"),
                candidate_status=str(item.get("candidate_status") or "current"),
            )
            for item in payload.get("positions") or []
            if isinstance(item, Mapping) and str(item.get("ticker") or "").strip()
        )
        watchlist = tuple(item for item in payload.get("watchlist") or [] if isinstance(item, Mapping))
        return cls(positions=positions, watchlist=watchlist)

    def to_dict(self) -> dict[str, Any]:
        return {
            "positions": [position.to_dict() for position in self.positions],
            "watchlist": [dict(item) for item in self.watchlist],
        }


def default_portfolio_state_path() -> Path:
    configured = os.getenv("NAVE_PORTFOLIO_STATE_FILE")
    return Path(configured).expanduser() if configured else Path.home() / ".nave" / "portfolio.json"


def load_portfolio_state(path: Path | None = None) -> PortfolioState:
    state_path = path or default_portfolio_state_path()
    if not state_path.exists():
        return PortfolioState()
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("portfolio state must be a JSON object")
    return PortfolioState.from_dict(payload)


def _result(
    workflow: str,
    status: ResearchStatus,
    payload: Mapping[str, Any],
    *,
    evidence: list[EvidenceReference] | None = None,
    warnings: list[str] | None = None,
    now: datetime | None = None,
) -> ResearchResult:
    decision_time = now or datetime.now(UTC)
    return ResearchResult(
        workflow=workflow,
        status=status,
        metadata=RunMetadata(
            strategy_name="portfolio-research",
            strategy_version="1.0.0",
            run_id=str(uuid.uuid4()),
            decision_time=decision_time,
            started_at=decision_time,
            completed_at=decision_time,
            input_available_at=decision_time,
        ),
        payload=payload,
        evidence=tuple(evidence or []),
        warnings=tuple(warnings or []),
    )


def review_positions(
    state: PortfolioState,
    evidence_by_ticker: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> ResearchResult:
    """Review each position and preserve the original thesis/provenance."""
    evidence_by_ticker = evidence_by_ticker or {}
    decisions: list[dict[str, Any]] = []
    evidence: list[EvidenceReference] = []
    for position in state.positions:
        ticker = position.ticker.upper()
        observed = evidence_by_ticker.get(ticker, {})
        reasons: list[str] = []
        if observed.get("invalidation") is True or position.candidate_status in {"invalidated", "broken"}:
            action = PortfolioAction.EXIT_CANDIDATE
            reasons.append("thesis_invalidated")
        elif observed.get("meaningful_new_information") is True:
            action = PortfolioAction.REVIEW_REQUIRED
            reasons.append("meaningful_new_information")
        elif observed.get("technical_condition") in {"weak", "breakdown"}:
            action = PortfolioAction.REDUCE_CANDIDATE
            reasons.append("technical_weakness")
        elif observed.get("macro_regime") in {None, "unknown", "UNKNOWN"}:
            action = PortfolioAction.REVIEW_REQUIRED
            reasons.append("macro_context_missing")
        else:
            action = PortfolioAction.HOLD
            reasons.append("thesis_and_current_evidence_have_no_recorded_break")
        decisions.append(
            {
                "ticker": ticker,
                "action": action,
                "thesis": position.thesis,
                "source_strategy": position.source_strategy,
                "watch_target": position.watch_target,
                "reasons": reasons,
                "evidence": dict(observed),
                "human_decision_required": True,
            }
        )
        evidence.append(
            EvidenceReference(
                reference_id=f"portfolio-position-{ticker}",
                source="private.portfolio.state",
                claim=f"Position state for {ticker} was supplied by the user-local portfolio file",
                kind=EvidenceKind.FACT,
                point_in_time=PointInTime(available_at=now, decision_time=now),
                metadata={"private_state": True},
            )
        )
    status = ResearchStatus.ACTION_REQUIRED if decisions else ResearchStatus.DATA_UNAVAILABLE
    result = _result(
        "portfolio.review",
        status,
        {"positions": decisions, "read_only": True, "human_decision_required": True},
        evidence=evidence,
        warnings=["position state is user-local and is not committed to Git"] if decisions else ["portfolio state is unavailable"],
        now=now,
    )
    return result


def portfolio_candidates(
    manufacturing: Mapping[str, Any],
    services: Mapping[str, Any],
    *,
    state: PortfolioState = PortfolioState(),
    additional_candidates: list[Mapping[str, Any]] | None = None,
    research_by_symbol: Mapping[str, Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> ResearchResult:
    """Rank ISM-derived candidates while keeping source provenance attached."""
    held = [position.ticker for position in state.positions if position.position_status == "open"]
    watched = [str(item.get("ticker") or "") for item in state.watchlist]
    pipeline = build_ism_equity_pipeline(
        manufacturing,
        services,
        portfolio_symbols=held,
        watch_symbols=watched,
        additional_candidates=additional_candidates or [],
        research_by_symbol=research_by_symbol,
        limit=6,
    )
    candidates = []
    evidence: list[EvidenceReference] = []
    decision_time = now or datetime.now(UTC)
    for item in pipeline.get("candidate_pool") or []:
        sources = sorted(set(item.get("sources") or []))
        why = [f"ISM {source} candidate" for source in sources] or ["explicitly supplied watch/portfolio candidate"]
        candidates.append(
            {
                "ticker": item.get("symbol"),
                "why_is_this_here": why,
                "provenance": {
                    "sources": sources,
                    "ism_signals": item.get("ism_signals") or [],
                    "portfolio_state": item.get("portfolio_state"),
                    "discovery_score": item.get("discovery_score"),
                },
                "status": "RESEARCH_REQUIRED",
                "research_only": True,
            }
        )
        evidence.append(
            EvidenceReference(
                reference_id=f"ism-candidate-{item.get('symbol')}",
                source="nave.trading.stocks.ism_equity_pipeline",
                claim=f"{item.get('symbol')} entered the bounded ISM candidate pool",
                kind=EvidenceKind.INFERENCE,
                point_in_time=PointInTime(decision_time=decision_time, available_at=decision_time),
                metadata={"sources": sources},
            )
        )
    status = ResearchStatus.SETUP_FOUND if candidates else ResearchStatus.NO_SETUP
    result = _result(
        "portfolio.candidates",
        status,
        {
            "candidates": candidates,
            "pipeline": pipeline,
            "why_is_this_here_required": True,
            "human_decision_required": True,
        },
        evidence=evidence,
        warnings=["ISM rankings are discovery context, not standalone buy signals"] if candidates else ["no candidate entered the bounded ISM pool"],
        now=now,
    )
    return result


def ism_rank(
    manufacturing: Mapping[str, Any],
    services: Mapping[str, Any],
    *,
    state: PortfolioState = PortfolioState(),
    now: datetime | None = None,
) -> ResearchResult:
    result = portfolio_candidates(manufacturing, services, state=state, now=now)
    payload = dict(result.payload)
    payload["mapping"] = "ISM industries → sectors → companies through the repo-native mapping/funnel"
    return ResearchResult(
        workflow="portfolio.ism",
        status=result.status,
        metadata=result.metadata,
        payload=payload,
        evidence=result.evidence,
        warnings=result.warnings,
    )


def check_watch(
    watches: list[Mapping[str, Any]],
    prices: Mapping[str, float],
    *,
    previous_prices: Mapping[str, float] | None = None,
    now: datetime | None = None,
) -> ResearchResult:
    """Cheap deterministic condition check; model escalation is always false."""
    events: list[dict[str, Any]] = []
    checked_prices: dict[str, float | None] = {}
    unavailable: list[str] = []
    previous_prices = previous_prices or {}
    for watch in watches:
        ticker = str(watch.get("ticker") or "").upper()
        price = prices.get(ticker)
        if not ticker:
            continue
        checked_prices[ticker] = float(price) if price is not None else None
        if price is None:
            unavailable.append(ticker)
        condition_raw = watch.get("condition")
        condition = str(condition_raw or "ZONE").upper()
        if condition not in {"ABOVE", "BELOW", "CROSS_ABOVE", "CROSS_BELOW", "ZONE"}:
            continue
        try:
            if price is None:
                continue
            current = float(price)
            threshold = watch.get("threshold")
            lower = upper = None
            zone = watch.get("zone")
            if isinstance(zone, (list, tuple)) and len(zone) == 2:
                lower, upper = float(zone[0]), float(zone[1])
            elif isinstance(watch.get("lower"), (int, float)) or isinstance(watch.get("upper"), (int, float)):
                lower = float(watch["lower"]) if watch.get("lower") is not None else None
                upper = float(watch["upper"]) if watch.get("upper") is not None else None
            if condition in {"ABOVE", "BELOW", "CROSS_ABOVE", "CROSS_BELOW"} and threshold is None:
                continue
            if condition == "ABOVE":
                reached = current >= float(threshold)
            elif condition == "BELOW":
                reached = current <= float(threshold)
            elif condition == "CROSS_ABOVE":
                previous = previous_prices.get(ticker)
                reached = previous is not None and float(previous) < float(threshold) <= current
            elif condition == "CROSS_BELOW":
                previous = previous_prices.get(ticker)
                reached = previous is not None and current <= float(threshold) < float(previous)
            else:
                # Backwards-compatible rows with only ``threshold`` retain
                # the former zone/reached behavior.
                reached = (
                    current >= float(threshold)
                    if not condition_raw and threshold is not None
                    else lower is not None and current >= lower and
                    upper is not None and current <= upper
                )
        except (TypeError, ValueError):
            continue
        if reached:
            event = "ZONE_REACHED" if condition == "ZONE" else condition
            item = {
                "ticker": ticker,
                "price": current,
                "condition": condition,
                "threshold": float(threshold) if threshold is not None else None,
                "thesis": watch.get("thesis"),
                "source_strategy": watch.get("source_strategy"),
                "event": event,
            }
            if lower is not None or upper is not None:
                item["zone"] = {"lower": lower, "upper": upper}
            events.append(item)
    result = _result(
        "portfolio.watch",
        ResearchStatus.ACTION_REQUIRED if events else ResearchStatus.NO_SETUP,
        {
            "events": events,
            "checked": len(watches),
            "prices": checked_prices,
            "unavailable_prices": unavailable,
            "model_escalation": False,
            "reason": "deterministic condition comparison only",
        },
        warnings=[
            *(["watch events notify a human; they never execute"] if events else []),
            *([f"current price unavailable for: {', '.join(unavailable)}"] if unavailable else []),
        ],
        now=now,
    )
    return result


class PortfolioWorkflow:
    def __init__(self, *, store: ResearchStore | None = None):
        self.store = store or ResearchStore()

    def save(self, result: ResearchResult) -> ResearchResult:
        self.store.save_result(result)
        return result

    def status(self) -> dict[str, Any]:
        output = {}
        for workflow in ("portfolio.review", "portfolio.candidates", "portfolio.ism", "portfolio.watch"):
            result = self.store.load_result(workflow)
            if result:
                output[workflow] = {"status": result.status.value, "run_id": result.metadata.run_id}
        return output
