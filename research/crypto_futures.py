"""Research-only crypto futures workflow over the existing momentum replay.

The existing momentum engine remains responsible for market calculations. This
module gives it a NAVE result contract, an explicit filter funnel, outcome
evaluation, and missed-move analysis. It never submits an order or creates a
trade alert.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from research.core.contracts import EvidenceKind, EvidenceReference, PointInTime, ResearchResult, ResearchStatus, RunMetadata
from research.core.store import ResearchStore
from trading.crypto.momentum.service import MomentumMarketService
from research.crypto_cot import COTContextProvider


STRATEGY_NAME = "crypto-futures-momentum-cot"
STRATEGY_VERSION = "1.0.0"


def _parse_time(value: Any, default: datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return default


def _asset_key(candidate: Mapping[str, Any]) -> str:
    return str(
        candidate.get("canonical_asset_id")
        or candidate.get("contract_address")
        or candidate.get("symbol")
        or candidate.get("asset")
        or "unknown"
    ).lower()


def _direction(candidate: Mapping[str, Any]) -> str | None:
    setup = candidate.get("setup_validation")
    if isinstance(setup, Mapping) and setup.get("direction") in {"long", "short"}:
        return str(setup["direction"])
    return None


def cot_regime_passes(regime: str, direction: str | None) -> bool:
    """Apply COT once as market/regime context, never as an altcoin signal."""
    normalized = regime.strip().lower()
    if normalized not in {"bullish", "bearish", "neutral"} or direction not in {"long", "short"}:
        return False
    if normalized == "bullish":
        return direction == "long"
    if normalized == "bearish":
        return direction == "short"
    return True


def _macro_context_passes(context: Mapping[str, Any] | None) -> bool:
    return bool(context and context.get("validated") is True)


def _candidate_evidence(
    candidate: Mapping[str, Any], *, observation_time: datetime, decision_time: datetime
) -> list[EvidenceReference]:
    features = candidate.get("features") if isinstance(candidate.get("features"), Mapping) else {}
    return [
        EvidenceReference(
            reference_id=f"crypto-{_asset_key(candidate)}-{observation_time.isoformat()}",
            source=str(features.get("data_source") or "nave.crypto.futures.replay"),
            claim="Momentum, market structure, and derivatives filters were evaluated at the observation time",
            kind=EvidenceKind.INFERENCE,
            confidence=(
                min(1.0, max(0.0, float(candidate.get("rank_score") or 0.0) / 100.0))
                if candidate.get("rank_score") is not None
                else None
            ),
            point_in_time=PointInTime(
                event_time=observation_time,
                available_at=_parse_time(features.get("data_timestamp"), observation_time),
                decision_time=decision_time,
            ),
            metadata={"asset": _asset_key(candidate)},
        )
    ]


def build_funnel(
    payload: Mapping[str, Any],
    *,
    macro_context: Mapping[str, Any] | None = None,
    cot_regime: str = "unknown",
    cot_context: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[EvidenceReference]]:
    """Build a transparent universe → candidate funnel from replay observations."""
    counts = {
        "universe": 0,
        "eligible": 0,
        "liquid": 0,
        "momentum_pass": 0,
        "derivatives_pass": 0,
        "macro_pass": 0,
        "cot_pass": 0,
        "final_candidates": 0,
    }
    final_candidates: list[dict[str, Any]] = []
    evidence: list[EvidenceReference] = []
    observations = payload.get("observations") or []
    unique_members: set[str] = set()
    effective_cot_regime = str(
        (cot_context or {}).get("regime") if cot_context is not None else cot_regime
    ).lower()
    if effective_cot_regime not in {"bullish", "bearish", "neutral"}:
        effective_cot_regime = "unknown"
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        observation_time = _parse_time(observation.get("observation_timestamp"), datetime.now(UTC))
        members = observation.get("universe_members_deduplicated") or observation.get("universe_members") or []
        for member in members:
            if isinstance(member, Mapping):
                member_key = _asset_key(member)
            else:
                member_key = str(member).strip().lower()
            if member_key and member_key != "unknown":
                unique_members.add(member_key)
        counts["universe"] = len(unique_members)
        candidates = observation.get("candidates") or []
        evidence.append(
            EvidenceReference(
                reference_id=f"crypto-universe-{observation_time.isoformat()}",
                source=str(observation.get("source") or "nave.crypto.futures.replay"),
                claim=f"The point-in-time universe contained {len(members)} deduplicated members",
                kind=EvidenceKind.FACT,
                confidence=1.0,
                point_in_time=PointInTime(
                    event_time=observation_time,
                    available_at=_parse_time(observation.get("source_timestamp"), observation_time)
                    if observation.get("source_timestamp")
                    else None,
                    decision_time=observation_time,
                ),
            )
        )
        for raw_candidate in candidates:
            if not isinstance(raw_candidate, Mapping):
                continue
            state = raw_candidate.get("ranking_state")
            liquidity = raw_candidate.get("liquidity") if isinstance(raw_candidate.get("liquidity"), Mapping) else {}
            eligible = state == "ELIGIBLE"
            liquid = liquidity.get("state") == "PASS"
            setup = raw_candidate.get("setup_validation")
            setup_valid = bool(isinstance(setup, Mapping) and setup.get("valid") is True)
            direction = _direction(raw_candidate)
            macro_pass = _macro_context_passes(macro_context)
            cot_pass = cot_regime_passes(effective_cot_regime, direction)
            if eligible:
                counts["eligible"] += 1
                counts["momentum_pass"] += 1
            if liquid:
                counts["liquid"] += 1
                counts["derivatives_pass"] += 1
            if macro_pass and eligible and liquid:
                counts["macro_pass"] += 1
            if cot_pass and eligible and liquid:
                counts["cot_pass"] += 1
            if not (eligible and liquid and setup_valid and macro_pass and cot_pass):
                continue
            features = raw_candidate.get("features") if isinstance(raw_candidate.get("features"), Mapping) else {}
            candidate = {
                "asset": raw_candidate.get("symbol"),
                "asset_key": _asset_key(raw_candidate),
                "direction": direction,
                "thesis": "momentum + market structure/derivatives + macro regime + market COT regime + validated Cava context",
                "evidence": raw_candidate.get("evidence") or {},
                "entry_research_zone": setup.get("entry_zone"),
                "invalidation": setup.get("invalidation"),
                "horizon": "configured forward outcome horizon",
                "confidence": min(1.0, max(0.0, float(raw_candidate.get("rank_score") or 0.0) / 100.0)),
                "major_risks": [
                    "derivatives liquidity can change before a human decision",
                    "COT is market/regime context and is not asset-specific",
                    "validated Cava context may become stale",
                ],
                "filters_passed": {
                    "universe": True,
                    "eligible": True,
                    "liquid": True,
                    "momentum_pass": True,
                    "derivatives_pass": True,
                    "macro_pass": True,
                    "cot_regime_pass": True,
                    "setup_validation": True,
                },
                "observation_timestamp": observation_time.isoformat(),
                "data_timestamp": features.get("data_timestamp"),
            }
            final_candidates.append(candidate)
            evidence.extend(_candidate_evidence(raw_candidate, observation_time=observation_time, decision_time=observation_time))
    counts["final_candidates"] = len(final_candidates)
    funnel = {
        **counts,
        "cot_regime": effective_cot_regime,
        "cot_scope": "market/regime context; no per-altcoin COT signal",
        "cot_context_status": (cot_context or {}).get("status", "MANUAL_OR_UNKNOWN"),
        "cot_source": (cot_context or {}).get("source"),
        "cot_as_of_date": (cot_context or {}).get("as_of_date"),
        "macro_context_validated": _macro_context_passes(macro_context),
        "validated_cava_context_consumed": _macro_context_passes(macro_context),
    }
    return funnel, final_candidates, evidence


def analyze_missed_moves(
    scan_payload: Mapping[str, Any],
    outcomes: list[Mapping[str, Any]],
    *,
    move_threshold: float = 0.20,
) -> list[dict[str, Any]]:
    """Identify large later moves without reading future data into the scan."""
    selected = {
        str(candidate.get("asset_key") or candidate.get("asset") or "").lower()
        for candidate in (scan_payload.get("final_candidates") or [])
        if isinstance(candidate, Mapping)
    }
    candidate_by_key: dict[str, Mapping[str, Any]] = {}
    for observation in scan_payload.get("observations") or []:
        if not isinstance(observation, Mapping):
            continue
        for candidate in observation.get("candidates") or []:
            if isinstance(candidate, Mapping):
                candidate_by_key[_asset_key(candidate)] = candidate
    missed: list[dict[str, Any]] = []
    for outcome in outcomes:
        if not isinstance(outcome, Mapping):
            continue
        forward_return = outcome.get("forward_return")
        try:
            move = float(forward_return)
        except (TypeError, ValueError):
            continue
        key = str(outcome.get("asset_key") or outcome.get("asset") or outcome.get("symbol") or "").lower()
        if move < move_threshold or key in selected:
            continue
        candidate = candidate_by_key.get(key)
        decision_time = _parse_time(
            (candidate or {}).get("observation_timestamp") or outcome.get("decision_time"),
            datetime.now(UTC),
        )
        available_at_raw = outcome.get("information_available_at")
        available_at = _parse_time(available_at_raw, decision_time) if available_at_raw else None
        if available_at is None:
            information_state = "UNKNOWN"
        else:
            information_state = "BEFORE_MOVE" if available_at <= decision_time else "AFTER_DECISION"
        rejection_filters: list[str] = []
        if candidate is None:
            rejection_filters.append("not_observed_in_scan_candidates")
        else:
            if candidate.get("ranking_state") != "ELIGIBLE":
                rejection_filters.append("momentum_or_rank")
            liquidity = candidate.get("liquidity") if isinstance(candidate.get("liquidity"), Mapping) else {}
            if liquidity.get("state") != "PASS":
                rejection_filters.append("derivatives_liquidity")
            setup = candidate.get("setup_validation")
            if not isinstance(setup, Mapping) or setup.get("valid") is not True:
                rejection_filters.append("market_structure_or_setup")
        missed.append(
            {
                "asset": outcome.get("asset") or outcome.get("symbol"),
                "asset_key": key,
                "decision_time": decision_time.isoformat(),
                "later_move": move,
                "information_existed_before_move": information_state,
                "universe_membership": outcome.get("universe_membership", candidate is not None),
                "rejection_filters": rejection_filters,
                "possible_systematic_blind_spot": (
                    outcome.get("possible_missing_feature")
                    or "candidate was rejected before the subsequent move; inspect the recorded features and filter threshold"
                ),
            }
        )
    return missed


class CryptoFuturesWorkflow:
    """Structured adapter around the existing deterministic crypto replay."""

    def __init__(self, *, store: ResearchStore | None = None):
        self.store = store or ResearchStore()

    def _result(
        self,
        *,
        workflow: str,
        status: ResearchStatus,
        payload: Mapping[str, Any],
        evidence: list[EvidenceReference] | None = None,
        warnings: list[str] | None = None,
        now: datetime | None = None,
    ) -> ResearchResult:
        decision_time = now or datetime.now(UTC)
        return ResearchResult(
            workflow=workflow,
            status=status,
            metadata=RunMetadata(
                strategy_name=STRATEGY_NAME,
                strategy_version=STRATEGY_VERSION,
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

    def scan_payload(
        self,
        replay_payload: Mapping[str, Any],
        *,
        macro_context: Mapping[str, Any] | None = None,
        cot_regime: str = "unknown",
        cot_context: Mapping[str, Any] | None = None,
        mode: str = "REPLAY",
        now: datetime | None = None,
    ) -> ResearchResult:
        funnel, candidates, evidence = build_funnel(
            replay_payload,
            macro_context=macro_context,
            cot_regime=cot_regime,
            cot_context=cot_context,
        )
        warnings = []
        if not funnel["macro_context_validated"]:
            warnings.append("validated Cava/macro context unavailable; final candidates are suppressed")
        if funnel["cot_regime"] not in {"bullish", "bearish", "neutral"}:
            warnings.append("COT regime unavailable; COT is not applied as an asset-level signal")
        warnings.extend(str(item) for item in (cot_context or {}).get("warnings", []))
        status = ResearchStatus.SETUP_FOUND if candidates else ResearchStatus.NO_SETUP
        result = self._result(
            workflow="crypto.futures.scan",
            status=status,
            payload={
                "strategy": STRATEGY_NAME,
                "strategy_version": STRATEGY_VERSION,
                "mode": mode,
                "funnel": funnel,
                "final_candidates": candidates,
                "observations": replay_payload.get("observations") or [],
                "raw_replay_summary": {
                    "window": replay_payload.get("window"),
                    "outcomes_persisted": False,
                },
                "research_only": True,
            },
            evidence=evidence,
            warnings=warnings,
            now=now,
        )
        self.store.save_result(result)
        self.store.save_context("crypto_futures_latest_scan", result.to_dict())
        return result

    def scan_live(
        self,
        *,
        service: MomentumMarketService | None = None,
        cot_provider: COTContextProvider | None = None,
        cot_regime: str | None = None,
        universe_size: int = 100,
        now: datetime | None = None,
    ) -> ResearchResult:
        """Run the normal current-universe path without replay arguments."""
        observed_at = now or datetime.now(UTC)
        market_service = service or MomentumMarketService()
        discovery = market_service.scan_current_universe_discovery(universe_size=universe_size)
        if discovery.get("status") != "OK":
            result = self._result(
                workflow="crypto.futures.scan",
                status=ResearchStatus.DATA_UNAVAILABLE,
                payload={
                    "strategy": STRATEGY_NAME,
                    "strategy_version": STRATEGY_VERSION,
                    "mode": "LIVE",
                    "funnel": {"universe": 0, "final_candidates": 0, "cot_regime": "unknown"},
                    "final_candidates": [],
                    "observations": [],
                    "provider": discovery,
                    "research_only": True,
                },
                warnings=["live current-universe discovery is unavailable", *discovery.get("unknowns", [])],
                now=observed_at,
            )
            self.store.save_result(result)
            return result
        cot_context = (cot_provider or COTContextProvider()).fetch(now=observed_at)
        if cot_regime is not None:
            cot_context = {
                **cot_context,
                "regime": cot_regime.lower(),
                "status": "OVERRIDE",
                "override": True,
                "warnings": ["COT regime was explicitly overridden for research"],
            }
        observation = {
            "observation_timestamp": discovery.get("observation_timestamp") or observed_at.isoformat(),
            "source": "live:CoinGecko+Hyperliquid",
            "source_timestamp": discovery.get("observation_timestamp") or observed_at.isoformat(),
            "universe_members_deduplicated": (discovery.get("universe") or {}).get("members", []),
            "candidates": discovery.get("candidates", []),
        }
        return self.scan_payload(
            {"observations": [observation], "window": {"mode": "LIVE"}},
            macro_context=self.store.load_context("cava"),
            cot_regime="unknown",
            cot_context=cot_context,
            mode="LIVE",
            now=observed_at,
        )

    def scan_from_fixture(
        self,
        *,
        fixture_path: str | Path,
        start: str,
        end: str,
        cadence: str = "6h",
        universe_size: int = 100,
        symbols: list[str] | None = None,
        macro_context: Mapping[str, Any] | None = None,
        cot_regime: str = "unknown",
    ) -> ResearchResult:
        replay = MomentumMarketService().research_universe_momentum_scan(
            fixture_path=str(fixture_path),
            start=start,
            end=end,
            cadence=cadence,
            universe_size=universe_size,
            symbols=symbols,
            validate_setups=True,
            include_sensitivity=False,
            max_candidates=100,
        )
        return self.scan_payload(
            replay,
            macro_context=macro_context,
            cot_regime=cot_regime,
            mode="REPLAY",
        )

    def evaluate(
        self,
        *,
        scan_result: ResearchResult,
        outcomes: list[Mapping[str, Any]],
        regime_key: str = "regime",
        hit_threshold: float = 0.0,
    ) -> ResearchResult:
        selected = {
            str(item.get("asset_key") or item.get("asset") or "").lower()
            for item in scan_result.payload.get("final_candidates", [])
            if isinstance(item, Mapping)
        }
        evaluated = []
        for outcome in outcomes:
            if not isinstance(outcome, Mapping):
                continue
            key = str(outcome.get("asset_key") or outcome.get("asset") or outcome.get("symbol") or "").lower()
            if key not in selected:
                continue
            try:
                forward_return = float(outcome["forward_return"])
            except (KeyError, TypeError, ValueError):
                continue
            evaluated.append({**dict(outcome), "asset_key": key, "hit": forward_return >= hit_threshold})
        hits = sum(bool(item["hit"]) for item in evaluated)
        by_regime: dict[str, list[float]] = defaultdict(list)
        for item in evaluated:
            by_regime[str(item.get(regime_key) or "UNKNOWN")].append(float(item["forward_return"]))
        metrics = {
            "selected_count": len(selected),
            "evaluated_count": len(evaluated),
            "hit_rate": hits / len(evaluated) if evaluated else None,
            "false_positive_rate": (len(evaluated) - hits) / len(evaluated) if evaluated else None,
            "mean_forward_return": (
                sum(float(item["forward_return"]) for item in evaluated) / len(evaluated)
                if evaluated
                else None
            ),
            "by_regime": {
                regime: {
                    "count": len(values),
                    "mean_forward_return": sum(values) / len(values),
                }
                for regime, values in by_regime.items()
            },
            "precision_recall": "not reported; this workflow evaluates selected forward outcomes and missed moves separately",
        }
        status = ResearchStatus.STRATEGY_NOT_VALIDATED
        result = self._result(
            workflow="crypto.futures.evaluate",
            status=status,
            payload={
                "strategy": STRATEGY_NAME,
                "strategy_version": scan_result.metadata.strategy_version,
                "metrics": metrics,
                "outcomes": evaluated,
                "source_scan_run_id": scan_result.metadata.run_id,
            },
            warnings=["evaluation is research evidence; validation requires a bounded out-of-sample sample"],
        )
        self.store.save_result(result)
        return result

    def missed_moves(
        self,
        *,
        scan_result: ResearchResult,
        outcomes: list[Mapping[str, Any]],
        move_threshold: float = 0.20,
    ) -> ResearchResult:
        missed = analyze_missed_moves(scan_result.payload, outcomes, move_threshold=move_threshold)
        result = self._result(
            workflow="crypto.futures.missed_moves",
            status=ResearchStatus.ACTION_REQUIRED if missed else ResearchStatus.NO_SETUP,
            payload={
                "strategy": STRATEGY_NAME,
                "strategy_version": scan_result.metadata.strategy_version,
                "move_threshold": move_threshold,
                "missed_moves": missed,
                "source_scan_run_id": scan_result.metadata.run_id,
            },
            warnings=["later outcomes are used only for audit; they are not fed back into the decision-time scan"],
        )
        self.store.save_result(result)
        return result

    def status(self) -> dict[str, Any]:
        return {
            workflow: {
                "status": result.status.value,
                "run_id": result.metadata.run_id,
                "strategy_version": result.metadata.strategy_version,
            }
            for workflow in (
                "crypto.futures.scan",
                "crypto.futures.evaluate",
                "crypto.futures.missed_moves",
            )
            if (result := self.store.load_result(workflow)) is not None
        }
