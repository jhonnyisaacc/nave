"""Deterministic historical replay for the crypto momentum discovery layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd

from trading.crypto.momentum.discovery import (
    DISCOVERY_HYPOTHESIS,
    AssetMarketData,
    DiscoveryCandidate,
    DiscoveryConfig,
    close_at,
    load_discovery_config,
    MarketDataProvider,
    normalize_frame,
    rank_universe,
)
from trading.crypto.momentum.engine import MomentumSetupEngine
from trading.crypto.momentum.universe import (
    FixtureUniverseProvider,
    UniverseMember,
    UniverseProviderUnavailable,
    as_utc,
    deduplicate_members,
    identity_key_for,
    members_for_target,
)


REPLAY_SCHEMA_VERSION = "crypto-momentum-discovery-replay.v1"
DEFAULT_REPLAY_TARGETS = ("ARB", "CAKE", "CRV", "TWT", "EDGE", "PONS")
ALLOWED_SETUP_CLASSIFICATIONS = {
    "PROMISING EXPLORATORY SIGNAL",
    "WEAK / UNSTABLE SIGNAL",
    "NO INCREMENTAL INFORMATION",
    "CONFOUNDED",
    "REGIME_DEPENDENT",
    "TEMPORALLY UNSTABLE",
    "INSUFFICIENT DATA",
    "BLOCKED BY OUTCOME COVERAGE",
}


@dataclass(frozen=True)
class PaperSetup:
    """Strict setup-validation result; it is not an execution instruction."""

    classification: str
    valid: bool
    direction: str | None
    entry_zone: list[float] | None
    invalidation: float | None
    targets: list[float]
    expected_move_pct: float | None
    net_rr: float | None
    estimated_cost_pct: float | None
    no_chase: bool | None
    blockers: tuple[str, ...] = ()
    data_complete: bool = True
    slippage_bps: float | None = None
    spread_bps: float | None = None

    def __post_init__(self) -> None:
        if self.classification not in ALLOWED_SETUP_CLASSIFICATIONS:
            raise ValueError(f"unsupported setup classification: {self.classification}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "valid": self.valid,
            "direction": self.direction,
            "entry_zone": self.entry_zone,
            "invalidation": self.invalidation,
            "targets": self.targets,
            "expected_move_pct": self.expected_move_pct,
            "net_rr": self.net_rr,
            "estimated_cost_pct": self.estimated_cost_pct,
            "no_chase": self.no_chase,
            "blockers": list(self.blockers),
            "data_complete": self.data_complete,
            "slippage_bps": self.slippage_bps,
            "spread_bps": self.spread_bps,
        }


class SetupValidator(Protocol):
    def validate(
        self,
        candidate: DiscoveryCandidate,
        market: AssetMarketData,
        observation_timestamp: datetime,
        config: DiscoveryConfig,
    ) -> PaperSetup:
        """Validate a paper setup without creating an action or alert."""


class ExistingMomentumSetupValidator:
    """Adapter from the existing strict BTC/ETH momentum engine to research output."""

    def __init__(self) -> None:
        self.engine = MomentumSetupEngine()

    def validate(
        self,
        candidate: DiscoveryCandidate,
        market: AssetMarketData,
        observation_timestamp: datetime,
        config: DiscoveryConfig,
    ) -> PaperSetup:
        daily = market.frame("1d")
        setup = market.frame("4h")
        trigger = market.frame("1h")
        if daily.empty or setup.empty or trigger.empty:
            return PaperSetup(
                "INSUFFICIENT DATA",
                False,
                None,
                None,
                None,
                [],
                None,
                None,
                None,
                None,
                ("missing_1d_4h_or_1h_setup_frame",),
                False,
            )
        try:
            plans = self.engine.evaluate_symbol(
                symbol=candidate.member.contract_symbol or candidate.member.symbol,
                daily_frame=daily.loc[daily.index <= pd.Timestamp(as_utc(observation_timestamp))],
                setup_frame=setup.loc[setup.index <= pd.Timestamp(as_utc(observation_timestamp))],
                trigger_frame=trigger.loc[trigger.index <= pd.Timestamp(as_utc(observation_timestamp))],
                open_interest=market.derivatives,
                funding_rate=_latest_funding(market.derivatives, observation_timestamp),
                as_of=pd.Timestamp(as_utc(observation_timestamp)),
                cot_overlay_mode="neutral",
            )
        except (KeyError, TypeError, ValueError, IndexError):
            return PaperSetup(
                "INSUFFICIENT DATA",
                False,
                None,
                None,
                None,
                [],
                None,
                None,
                None,
                None,
                ("existing_momentum_validator_failed_closed",),
                False,
            )
        if not plans:
            return PaperSetup(
                "WEAK / UNSTABLE SIGNAL",
                False,
                None,
                None,
                None,
                [],
                None,
                None,
                None,
                None,
                ("existing_momentum_engine_returned_no_plan",),
            )
        plan = sorted(
            plans,
            key=lambda item: (bool(item.tradeable), item.setup_status == "confirmed", item.confidence_score),
            reverse=True,
        )[0]
        zone = [float(value) for value in plan.entry_zone] if plan.entry_zone else None
        entry = _entry_from_zone(zone, plan.side)
        price = candidate.features.get("price")
        no_chase = no_chase_allowed(price, entry, plan.side, config.retest_distance_pct)
        valid = bool(
            candidate.liquidity.state == "PASS"
            and plan.tradeable
            and plan.setup_status in {"confirmed", "pending"}
            and no_chase
            and plan.invalidation is not None
            and plan.tp1 is not None
            and plan.tp2 is not None
            and plan.tp3 is not None
        )
        blockers: list[str] = []
        if candidate.liquidity.state != "PASS":
            blockers.append("liquidity_not_passed")
        if not plan.tradeable:
            blockers.append("existing_momentum_engine_not_tradeable")
        if plan.setup_status not in {"confirmed", "pending"}:
            blockers.append("4h_or_1h_setup_not_confirmed")
        if no_chase is False:
            blockers.append("chase_condition")
        return PaperSetup(
            "PROMISING EXPLORATORY SIGNAL" if valid else "WEAK / UNSTABLE SIGNAL",
            valid,
            plan.side,
            zone,
            float(plan.invalidation) if plan.invalidation is not None else None,
            [float(value) for value in (plan.tp1, plan.tp2, plan.tp3) if value is not None],
            float(plan.expected_move_pct),
            _net_rr(
                entry,
                float(plan.invalidation) if plan.invalidation is not None else None,
                float(plan.tp2) if plan.tp2 is not None else None,
                _latest_funding(market.derivatives, observation_timestamp),
                config,
                slippage_bps=candidate.liquidity.slippage_bps,
                spread_bps=candidate.liquidity.spread_bps,
            ),
            _cost_pct(
                config,
                _latest_funding(market.derivatives, observation_timestamp),
                1,
                slippage_bps=candidate.liquidity.slippage_bps,
                spread_bps=candidate.liquidity.spread_bps,
            ),
            no_chase,
            tuple(blockers),
            True,
            candidate.liquidity.slippage_bps,
            candidate.liquidity.spread_bps,
        )


@dataclass(frozen=True)
class ReplayFixture:
    metadata: dict[str, Any]
    universe_provider: FixtureUniverseProvider
    market_data: dict[str, AssetMarketData]


class FixtureMarketDataProvider:
    """Canonical-ID keyed market data provider for deterministic local replay."""

    def __init__(self, market_data: dict[str, AssetMarketData]) -> None:
        self.market_data = market_data

    def data_for(self, member: UniverseMember) -> AssetMarketData | None:
        if not member.identity_key:
            return None
        return self.market_data.get(member.identity_key)


def load_replay_fixture(path: str | Path) -> ReplayFixture:
    """Load raw-row or compact deterministic-series fixture data."""
    fixture_path = Path(path)
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") not in {None, "crypto-momentum-replay-fixture.v1"}:
        raise ValueError("unsupported crypto momentum fixture schema")
    raw_assets = payload.get("market_data")
    if not isinstance(raw_assets, dict):
        raise ValueError("fixture must contain market_data keyed by canonical asset ID")
    market_data: dict[str, AssetMarketData] = {}
    for raw_key, raw_asset in raw_assets.items():
        if not isinstance(raw_asset, dict):
            continue
        canonical_id = raw_asset.get("canonical_asset_id")
        contract_address = raw_asset.get("contract_address")
        identity_key = (
            f"asset:{str(canonical_id).lower()}"
            if canonical_id
            else f"contract:{str(contract_address).lower()}"
            if contract_address
            else f"asset:{str(raw_key).lower()}"
        )
        frames: dict[str, pd.DataFrame] = {}
        for timeframe, raw_series in (raw_asset.get("candles") or {}).items():
            frames[str(timeframe)] = _series_to_frame(raw_series, default_interval=timeframe)
        derivatives = _series_to_frame(
            raw_asset.get("derivatives"), default_interval="6h", derivative=True
        )
        source_timestamp = raw_asset.get("source_timestamp")
        market = AssetMarketData(
            frames=frames,
            derivatives=derivatives,
            source=str(raw_asset.get("source") or payload.get("metadata", {}).get("source") or "fixture"),
            source_timestamp=as_utc(source_timestamp) if source_timestamp else None,
        )
        market_data[identity_key] = market
    return ReplayFixture(
        metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        universe_provider=FixtureUniverseProvider.from_path(fixture_path),
        market_data=market_data,
    )


class UniverseMomentumReplay:
    """Run a no-look-ahead discovery replay and conservative paper outcome audit."""

    def __init__(
        self,
        universe_provider: Any,
        market_provider: MarketDataProvider,
        *,
        config: DiscoveryConfig | None = None,
        setup_validator: SetupValidator | None = None,
    ) -> None:
        self.universe_provider = universe_provider
        self.market_provider = market_provider
        self.config = config or load_discovery_config()
        self.setup_validator = setup_validator or ExistingMomentumSetupValidator()

    def run(
        self,
        *,
        start: datetime | str,
        end: datetime | str,
        cadence: str | int | None = None,
        universe_size: int = 100,
        symbols: list[str] | None = None,
        validate_setups: bool = True,
        include_sensitivity: bool = True,
        max_candidates: int = 25,
    ) -> dict[str, Any]:
        start_at = as_utc(start)
        end_at = as_utc(end)
        if end_at < start_at:
            raise ValueError("end must not be before start")
        if universe_size <= 0:
            raise ValueError("universe_size must be positive")
        cadence_delta = parse_cadence(cadence or self.config.cadence_hours)
        observations = self._run_core(
            start_at=start_at,
            end_at=end_at,
            cadence_delta=cadence_delta,
            universe_size=universe_size,
            symbols=symbols,
            validate_setups=validate_setups,
            max_candidates=max_candidates,
        )
        first_records = _first_eligible_records(
            observations, min_rank_score=self.config.min_rank_score
        )
        outcomes = [
            self._resolve_outcome(record, validate_setups=validate_setups)
            for record in first_records.values()
        ]
        metrics = summarize_replay(observations, outcomes, self.config, window_start=start_at)
        target_report = self._target_report(
            list(symbols) if symbols is not None else list(DEFAULT_REPLAY_TARGETS),
            observations,
            first_records,
            outcomes,
        )
        payload: dict[str, Any] = {
            "schema_version": REPLAY_SCHEMA_VERSION,
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "mode": "historical_research_only",
            "hypothesis": DISCOVERY_HYPOTHESIS,
            "fact_inference_hypothesis_unknown_contract": {
                "facts": "observed market/universe/contract data and calculated indicators",
                "inferences": "ranked candidate, liquidity pass, or paper setup assessment",
                "hypothesis": "discovery layer may improve opportunity coverage",
                "unknown": "provider gaps, ambiguous identity, unavailable contracts, and incomplete outcomes remain explicit",
            },
            "assumptions": {
                "publication_time_rule": "A universe/data observation is usable only when available_at/source_timestamp is <= observation time.",
                "current_universe_fallback": False,
                "cadence": cadence
                if cadence is not None
                else f"{cadence_delta.total_seconds() / 3600:g}h",
                "universe_size": universe_size,
                "source": "provider_contract_or_offline_fixture",
                "execution": "paper-only; no orders, alerts, wallets, or permissions are touched",
            },
            "window": {"start": start_at.isoformat(), "end": end_at.isoformat()},
            "config": _config_dict(self.config),
            "observations": observations,
            "first_eligible": list(first_records.values()),
            "outcomes": outcomes,
            "metrics": metrics,
            "target_report": target_report,
        }
        if include_sensitivity:
            payload["sensitivity"] = self._sensitivity(
                start_at, end_at, universe_size, symbols, validate_setups, max_candidates
            )
        return payload

    def _run_core(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        cadence_delta: pd.Timedelta,
        universe_size: int,
        symbols: list[str] | None,
        validate_setups: bool,
        max_candidates: int,
        config: DiscoveryConfig | None = None,
    ) -> list[dict[str, Any]]:
        cfg = config or self.config
        wanted = {value.strip().upper() for value in (symbols or []) if value.strip()}
        current = pd.Timestamp(start_at)
        output: list[dict[str, Any]] = []
        while current <= pd.Timestamp(end_at):
            as_of = current.to_pydatetime()
            observation: dict[str, Any] = {
                "observation_timestamp": as_of.isoformat(),
                "status": "OK",
                "candidates": [],
                "source": None,
                "source_timestamp": None,
            }
            try:
                snapshot = self.universe_provider.snapshot_at(as_of, universe_size=universe_size)
            except UniverseProviderUnavailable as exc:
                observation["status"] = "PROVIDER_UNAVAILABLE"
                observation["unknowns"] = [str(exc)]
                output.append(observation)
                current += cadence_delta
                continue
            members = deduplicate_members(snapshot.members)
            if not snapshot.point_in_time_valid:
                observation["status"] = "INCOMPLETE_DATA"
                observation["unknowns"] = [snapshot.validity_note or "point_in_time_snapshot_invalid"]
            observation["source"] = snapshot.source
            observation["source_timestamp"] = (
                snapshot.source_timestamp.isoformat() if snapshot.source_timestamp else None
            )
            # Preserve every venue observation for auditability; the candidate
            # ranking below still uses the canonical-identity deduplicated set.
            observation["universe_members"] = [member.to_dict() for member in snapshot.members]
            observation["universe_members_deduplicated"] = [member.to_dict() for member in members]
            if not snapshot.point_in_time_valid:
                observation["candidate_count"] = 0
                observation["eligible_count"] = 0
                output.append(observation)
                current += cadence_delta
                continue
            data_by_identity: dict[str, AssetMarketData] = {}
            for member in members:
                if not member.identity_key:
                    continue
                market = self.market_provider.data_for(member)
                if market is not None:
                    data_by_identity[member.identity_key] = market
            candidates = rank_universe(
                members,
                data_by_identity,
                as_of,
                config=cfg,
            )
            serializable_candidates: list[dict[str, Any]] = []
            for candidate in candidates:
                if wanted and not _candidate_matches(candidate, wanted):
                    continue
                serialized = candidate.to_dict()
                if candidate.eligible and validate_setups and candidate.member.identity_key:
                    market = data_by_identity.get(candidate.member.identity_key)
                    if market is not None:
                        serialized["setup_validation"] = self.setup_validator.validate(
                            candidate, market, as_of, cfg
                        ).to_dict()
                serializable_candidates.append(serialized)
            # Keep the complete observation for reproducibility.  The capped
            # view is only for human renderers; eligibility accounting must see
            # every member in the configured universe.
            observation["candidates"] = serializable_candidates
            observation["top_candidates"] = serializable_candidates[:max_candidates]
            observation["candidate_count"] = len(serializable_candidates)
            observation["eligible_count"] = sum(
                1 for item in serializable_candidates if item.get("ranking_state") == "ELIGIBLE"
            )
            output.append(observation)
            current += cadence_delta
        return output

    def _resolve_outcome(self, record: dict[str, Any], *, validate_setups: bool) -> dict[str, Any]:
        as_of = as_utc(record["first_eligible_at"])
        member = _member_from_record(record)
        market = self.market_provider.data_for(member)
        base = {
            "symbol": member.symbol,
            "canonical_asset_id": member.canonical_asset_id,
            "contract_address": member.contract_address,
            "detection_timestamp": as_of.isoformat(),
            "data_timestamp": record.get("data_timestamp"),
            "status": "INCOMPLETE_DATA",
            "forward_return": None,
            "mfe": None,
            "mae": None,
            "meaningful_move": None,
            "paper_trade": None,
        }
        if market is None:
            base["status"] = "PROVIDER_UNAVAILABLE"
            return base
        frame = market.frame("1h")
        current_price = record.get("features", {}).get("price")
        if current_price is None:
            base["status"] = "INCOMPLETE_DATA"
            return base
        future_end = as_of + pd.Timedelta(hours=self.config.outcome_horizon_hours)
        future = frame.loc[(frame.index > pd.Timestamp(as_of)) & (frame.index <= pd.Timestamp(future_end))]
        close, close_timestamp = close_at(frame, future_end)
        if future.empty or close is None or close_timestamp is None or close_timestamp < pd.Timestamp(future_end):
            base["status"] = "BLOCKED BY OUTCOME COVERAGE"
            base["unknowns"] = ["forward_outcome_window_incomplete"]
            return base
        if not {"high", "low"}.issubset(future.columns):
            base["status"] = "INCOMPLETE_DATA"
            base["unknowns"] = ["forward_ohlc_high_low_unavailable"]
            return base
        side = None
        setup_raw = record.get("setup_validation") if validate_setups else None
        if isinstance(setup_raw, dict):
            side = setup_raw.get("direction")
        side = side if side in {"long", "short"} else "long"
        sign = 1.0 if side == "long" else -1.0
        forward_return = (close / float(current_price) - 1.0) * sign
        try:
            highs = future["high"].astype(float)
            lows = future["low"].astype(float)
        except (TypeError, ValueError):
            base["status"] = "INCOMPLETE_DATA"
            base["unknowns"] = ["forward_ohlc_numeric_data_invalid"]
            return base
        mfe = (float(highs.max()) / float(current_price) - 1.0) if side == "long" else (
            float(current_price) / float(lows.min()) - 1.0
        )
        mae = (float(lows.min()) / float(current_price) - 1.0) if side == "long" else (
            float(current_price) / float(highs.max()) - 1.0
        )
        base.update(
            {
                "status": "COMPLETED",
                "side": side,
                "forward_return": forward_return,
                "mfe": mfe,
                "mae": mae,
                "meaningful_move": forward_return >= self.config.meaningful_move_pct,
                "outcome_timestamp": close_timestamp.isoformat(),
                "regime": _regime(record.get("features", {}).get("universe_benchmark_return")),
            }
        )
        if isinstance(setup_raw, dict) and setup_raw.get("valid") is True:
            setup = PaperSetup(**_paper_setup_kwargs(setup_raw))
            base["paper_trade"] = simulate_paper_setup(
                setup,
                future,
                config=self.config,
                funding_rate=_latest_funding(market.derivatives, as_of),
            )
        return base

    def _target_report(
        self,
        symbols: list[str],
        observations: list[dict[str, Any]],
        first_records: dict[str, dict[str, Any]],
        outcomes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not symbols:
            return []
        by_identity_outcome = {
            identity_key_for(item.get("canonical_asset_id"), item.get("contract_address")): item
            for item in outcomes
            if identity_key_for(item.get("canonical_asset_id"), item.get("contract_address"))
        }
        observed_members: list[UniverseMember] = []
        for observation in observations:
            # Preserve target metadata independently of candidate display limits.
            for member in observation.get("universe_members", []):
                observed_members.append(_member_from_record(member))
        deduped = deduplicate_members(observed_members)
        reports: list[dict[str, Any]] = []
        for target in symbols:
            matches = members_for_target(deduped, target)
            metadata_matches = members_for_target(tuple(observed_members), target)
            reported_members = metadata_matches or matches
            report: dict[str, Any] = {
                "target": target.upper(),
                "canonical_asset_ids": sorted({m.canonical_asset_id for m in reported_members if m.canonical_asset_id}),
                "venues": sorted({m.venue for m in reported_members if m.venue}),
                "contract_symbols": sorted({m.contract_symbol for m in reported_members if m.contract_symbol}),
                "top_100_observed": any(m.is_top_ranked for m in reported_members),
                "liquid_perpetual_observed": any(
                    m.universe_source == "liquid_perpetual" for m in reported_members
                ),
                "status": None,
                "first_eligible_at": None,
            }
            if len(matches) > 1 and not _target_is_canonical_id(target, matches):
                report["status"] = "UNKNOWN_ASSET_IDENTITY"
            elif not matches:
                report["status"] = "UNKNOWN_ASSET_IDENTITY"
            else:
                identity = matches[0].identity_key
                record = first_records.get(identity or "")
                if not any(m.is_top_ranked for m in matches):
                    report["status"] = "OUTSIDE_HISTORICAL_TOP_100"
                elif record is not None:
                    report["status"] = "DETECTED_FIRST_ELIGIBLE"
                    report["first_eligible_at"] = record.get("first_eligible_at")
                    if identity and identity in by_identity_outcome:
                        report["outcome"] = by_identity_outcome[identity]
                else:
                    statuses = [
                        candidate.get("liquidity", {}).get("state")
                        for observation in observations
                        for candidate in observation.get("candidates", [])
                        if identity_key_for(
                            candidate.get("canonical_asset_id"), candidate.get("contract_address")
                        )
                        == matches[0].identity_key
                    ]
                    if "REJECT" in statuses:
                        report["status"] = "FUTURES_LIQUIDITY_INSUFFICIENT"
                    elif "UNKNOWN" in statuses:
                        report["status"] = "INCOMPLETE_DATA"
                    else:
                        report["status"] = "NO_ELIGIBLE_DETECTION"
            reports.append(report)
        return reports

    def _sensitivity(
        self,
        start: datetime,
        end: datetime,
        universe_size: int,
        symbols: list[str] | None,
        validate_setups: bool,
        max_candidates: int,
    ) -> list[dict[str, Any]]:
        results = []
        for cadence_hours in self.config.sensitivity_cadence_hours:
            observations = self._run_core(
                start_at=start,
                end_at=end,
                cadence_delta=parse_cadence(cadence_hours),
                universe_size=universe_size,
                symbols=symbols,
                validate_setups=validate_setups,
                max_candidates=max_candidates,
            )
            base_first = _first_eligible_records(
                observations, min_rank_score=self.config.min_rank_score
            )
            base_outcomes = [
                self._resolve_outcome(record, validate_setups=validate_setups)
                for record in base_first.values()
            ]
            base_summary = summarize_replay(
                observations,
                base_outcomes,
                self.config,
                window_start=start,
                min_rank_score=self.config.min_rank_score,
            )
            for threshold in self.config.sensitivity_score_thresholds:
                first_at_threshold = _first_eligible_records(
                    observations, min_rank_score=threshold
                )
                threshold_outcomes = [
                    self._resolve_outcome(record, validate_setups=validate_setups)
                    for record in first_at_threshold.values()
                ]
                summary = summarize_replay(
                    observations,
                    threshold_outcomes,
                    self.config,
                    window_start=start,
                    min_rank_score=threshold,
                )
                completed = [item for item in threshold_outcomes if item.get("status") == "COMPLETED"]
                meaningful = [item for item in completed if item.get("meaningful_move") is True]
                results.append(
                    {
                        "cadence_hours": cadence_hours,
                        "min_rank_score": threshold,
                        "eligible_detections": sum(
                            _eligible_count(observation, min_rank_score=threshold)
                            for observation in observations
                        ),
                        "outcome_coverage": round(
                            len(completed) / len(first_at_threshold), 4
                        )
                        if first_at_threshold
                        else 0.0,
                        "meaningful_move_precision": round(len(meaningful) / len(completed), 4) if completed else 0.0,
                        "base_summary": base_summary,
                        "threshold_summary": summary,
                    }
                )
        return results


def parse_cadence(value: str | int | float) -> pd.Timedelta:
    if isinstance(value, (int, float)):
        if value <= 0:
            raise ValueError("cadence must be positive")
        return pd.Timedelta(hours=float(value))
    try:
        parsed = pd.Timedelta(value)
    except ValueError as exc:
        raise ValueError("cadence must be a positive duration such as 6h") from exc
    if parsed <= pd.Timedelta(0):
        raise ValueError("cadence must be positive")
    return parsed


def simulate_paper_setup(
    setup: PaperSetup,
    future_frame: pd.DataFrame,
    *,
    config: DiscoveryConfig,
    funding_rate: float | None,
) -> dict[str, Any]:
    """Simulate a limit-zone paper setup with conservative OHLC execution.

    The entry must actually trade through the selected edge of the entry zone;
    otherwise the result is an explicit ``NO_FILL`` rather than a completed
    trade.  Once filled, a candle touching both stop and target is resolved to
    the stop first because intrabar ordering is unknown.
    """
    future_frame = normalize_frame(future_frame)
    if not setup.valid or setup.direction not in {"long", "short"} or not setup.entry_zone:
        return {"status": "INSUFFICIENT DATA", "r_multiple": None}
    if (
        setup.invalidation is None
        or len(setup.targets) < 3
        or future_frame.empty
        or not {"high", "low", "close"}.issubset(future_frame.columns)
    ):
        return {"status": "INSUFFICIENT DATA", "r_multiple": None}
    side = setup.direction
    raw_entry = _entry_from_zone(setup.entry_zone, side)
    if raw_entry is None:
        return {"status": "INSUFFICIENT DATA", "r_multiple": None}
    try:
        stop = float(setup.invalidation)
        tp2 = float(setup.targets[1])
        if raw_entry <= 0 or stop <= 0 or tp2 <= 0:
            return {"status": "INSUFFICIENT DATA", "r_multiple": None}
    except (TypeError, ValueError):
        return {"status": "INSUFFICIENT DATA", "r_multiple": None}
    if (side == "long" and not stop < raw_entry < tp2) or (
        side == "short" and not stop > raw_entry > tp2
    ):
        return {"status": "INSUFFICIENT DATA", "r_multiple": None}

    slippage_bps = (
        setup.slippage_bps
        if setup.slippage_bps is not None
        else config.default_slippage_bps
    )
    spread_bps = setup.spread_bps if setup.spread_bps is not None else 0.0
    try:
        slippage_bps = float(slippage_bps)
        spread_bps = float(spread_bps)
    except (TypeError, ValueError):
        return {"status": "INSUFFICIENT DATA", "r_multiple": None}
    if slippage_bps < 0 or spread_bps < 0 or config.funding_interval_hours <= 0:
        return {"status": "INSUFFICIENT DATA", "r_multiple": None}

    slip = slippage_bps / 10_000
    fee = config.fee_bps_per_side / 10_000
    fill_position = None
    fill_timestamp = None
    for position, (timestamp, row) in enumerate(future_frame.iterrows()):
        try:
            high = float(row["high"])
            low = float(row["low"])
        except (TypeError, ValueError):
            return {"status": "INSUFFICIENT DATA", "r_multiple": None}
        if low <= raw_entry <= high:
            fill_position = position
            fill_timestamp = timestamp
            break
    if fill_position is None or fill_timestamp is None:
        return {
            "status": "NO_FILL",
            "r_multiple": None,
            "reason": "entry_zone_not_reached",
        }

    entry = raw_entry * (1 + slip if side == "long" else 1 - slip)
    stop_distance = abs(raw_entry - stop)
    if stop_distance <= 0:
        return {"status": "INSUFFICIENT DATA", "r_multiple": None}

    exit_mid = None
    exit_price = None
    exit_timestamp = None
    exit_reason = "time_limit"
    filled_frame = future_frame.iloc[fill_position:]
    for timestamp, row in filled_frame.iterrows():
        try:
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
        except (TypeError, ValueError):
            return {"status": "INSUFFICIENT DATA", "r_multiple": None}
        if side == "long":
            # Stop first if both levels occur in one candle.
            if low <= stop:
                exit_mid = stop
                exit_price = stop * (1 - slip)
                exit_timestamp = timestamp
                exit_reason = "stop"
                break
            if high >= tp2:
                exit_mid = tp2
                exit_price = tp2 * (1 - slip)
                exit_timestamp = timestamp
                exit_reason = "tp2"
                break
        else:
            if high >= stop:
                exit_mid = stop
                exit_price = stop * (1 + slip)
                exit_timestamp = timestamp
                exit_reason = "stop"
                break
            if low <= tp2:
                exit_mid = tp2
                exit_price = tp2 * (1 + slip)
                exit_timestamp = timestamp
                exit_reason = "tp2"
                break
        exit_mid = close
        exit_price = close * (1 - slip if side == "long" else 1 + slip)
        exit_timestamp = timestamp
    if exit_mid is None or exit_price is None or exit_timestamp is None:
        return {"status": "INSUFFICIENT DATA", "r_multiple": None}
    holding_hours = max(
        0.0,
        (pd.Timestamp(exit_timestamp) - pd.Timestamp(fill_timestamp)).total_seconds() / 3600,
    )
    gross_return = (
        (exit_mid - raw_entry) / raw_entry
        if side == "long"
        else (raw_entry - exit_mid) / raw_entry
    )
    funding_cost = (funding_rate or 0.0) * holding_hours / config.funding_interval_hours
    signed_funding = funding_cost if side == "long" else -funding_cost
    fee_impact = 2 * fee
    slippage_impact = 2 * slip
    spread_impact = spread_bps / 10_000
    total_cost = fee_impact + slippage_impact + spread_impact + signed_funding
    net_return = gross_return - total_cost
    r_multiple = net_return / (stop_distance / raw_entry)
    return {
        "status": "COMPLETED",
        "side": side,
        "fill_timestamp": pd.Timestamp(fill_timestamp).isoformat(),
        "entry_price": entry,
        "exit_price": exit_price,
        "exit_timestamp": pd.Timestamp(exit_timestamp).isoformat(),
        "exit_reason": exit_reason,
        "holding_hours": holding_hours,
        "gross_return": gross_return,
        "fee_impact": fee_impact,
        "slippage_impact": slippage_impact,
        "spread_impact": spread_impact,
        "funding_impact": signed_funding,
        "slippage_bps": slippage_bps,
        "spread_bps": spread_bps,
        "net_return": net_return,
        "r_multiple": r_multiple,
        "turnover_notional_units": 2.0,
    }


def summarize_replay(
    observations: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    config: DiscoveryConfig,
    *,
    window_start: datetime | None = None,
    min_rank_score: float | None = None,
) -> dict[str, Any]:
    eligible = (
        sum(
            _eligible_count(observation, min_rank_score=min_rank_score)
            for observation in observations
        )
        if min_rank_score is not None
        else sum(int(item.get("eligible_count", 0)) for item in observations)
    )
    first_eligible_count = len(outcomes)
    completed = [item for item in outcomes if item.get("status") == "COMPLETED"]
    meaningful = [item for item in completed if item.get("meaningful_move") is True]
    false_positives = [item for item in completed if item.get("meaningful_move") is False]
    trades = [
        item["paper_trade"]
        for item in completed
        if isinstance(item.get("paper_trade"), dict)
        and item["paper_trade"].get("status") == "COMPLETED"
    ]
    r_values = [float(item.get("r_multiple")) for item in trades if item.get("r_multiple") is not None]
    cumulative = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in r_values:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    by_asset: dict[str, dict[str, Any]] = {}
    for item in completed:
        symbol = str(item.get("symbol") or "?")
        bucket = by_asset.setdefault(symbol, {"outcomes": 0, "meaningful_moves": 0, "paper_trades": 0, "r_values": []})
        bucket["outcomes"] += 1
        bucket["meaningful_moves"] += int(item.get("meaningful_move") is True)
        if isinstance(item.get("paper_trade"), dict) and item["paper_trade"].get("r_multiple") is not None:
            bucket["paper_trades"] += 1
            bucket["r_values"].append(item["paper_trade"]["r_multiple"])
    for bucket in by_asset.values():
        bucket["expectancy_r"] = round(float(np.mean(bucket["r_values"])), 4) if bucket["r_values"] else None
        bucket.pop("r_values", None)
    by_regime: dict[str, dict[str, int]] = {}
    for item in completed:
        regime = str(item.get("regime") or "UNKNOWN")
        bucket = by_regime.setdefault(regime, {"outcomes": 0, "meaningful_moves": 0})
        bucket["outcomes"] += 1
        bucket["meaningful_moves"] += int(item.get("meaningful_move") is True)
    return {
        "candidate_coverage": {
            "eligible_detections": eligible,
            "unique_candidates_first_eligible": first_eligible_count,
            "outcome_windows_complete": len(completed),
            "outcome_coverage": round(len(completed) / first_eligible_count, 4)
            if first_eligible_count
            else 0.0,
        },
        "meaningful_move_definition": {
            "forward_return_gte": config.meaningful_move_pct,
            "directional": True,
            "horizon_hours": config.outcome_horizon_hours,
            "liquidity_gate": "candidate must pass the configured liquidity gate",
        },
        "detection_latency": {
            "first_eligible_records": len(outcomes),
            "mean_hours_from_window_start": round(
                _mean_detection_latency(outcomes, window_start=window_start), 4
            ),
        },
        "precision": {
            "meaningful_move_precision": round(len(meaningful) / len(completed), 4) if completed else 0.0,
            "false_positives": len(false_positives),
        },
        "mfe": _mean_field(completed, "mfe"),
        "mae": _mean_field(completed, "mae"),
        "paper_trading": {
            "trade_count": len(trades),
            "expectancy_r": round(float(np.mean(r_values)), 4) if r_values else 0.0,
            "max_drawdown_r": round(drawdown, 4),
            "turnover_notional_units": round(sum(float(item.get("turnover_notional_units", 0.0)) for item in trades), 4),
            "fee_impact": round(sum(float(item.get("fee_impact", 0.0)) for item in trades), 6),
            "funding_impact": round(sum(float(item.get("funding_impact", 0.0)) for item in trades), 6),
            "slippage_impact": round(sum(float(item.get("slippage_impact", 0.0)) for item in trades), 6),
        },
        "performance_by_asset": by_asset,
        "performance_by_regime": by_regime,
    }


def _series_to_frame(raw: Any, *, default_interval: str, derivative: bool = False) -> pd.DataFrame:
    if raw is None:
        return pd.DataFrame()
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict) and isinstance(raw.get("rows"), list):
        rows = raw["rows"]
    elif isinstance(raw, dict) and raw.get("start") and raw.get("periods"):
        start = pd.Timestamp(as_utc(raw["start"]))
        interval_text = str(raw.get("interval") or default_interval)
        if interval_text.lower().endswith("d"):
            interval_text = interval_text[:-1] + "D"
        interval = pd.Timedelta(interval_text)
        periods = int(raw["periods"])
        rows = []
        close = float(raw.get("base", 1.0))
        trend = float(raw.get("trend_per_bar", 0.0))
        wave = float(raw.get("wave_amplitude", 0.0))
        bar_range = float(raw.get("bar_range", 0.002))
        volume = float(raw.get("volume_base", 1_000_000.0))
        volume_trend = float(raw.get("volume_trend_per_bar", 0.0))
        for index in range(periods):
            open_price = close
            close = float(raw.get("base", 1.0)) * (1 + trend * index + wave * np.sin(index / 5))
            rows.append(
                {
                    "timestamp": (start + index * interval).isoformat(),
                    "open": open_price,
                    "high": max(open_price, close) * (1 + bar_range),
                    "low": min(open_price, close) * (1 - bar_range),
                    "close": close,
                    "volume": volume * (1 + volume_trend * index),
                }
            )
        if derivative:
            rows = [
                {
                    "timestamp": (start + index * interval).isoformat(),
                    "quote_volume_24h": raw.get("quote_volume_24h"),
                    "open_interest": raw.get("open_interest"),
                    "funding_rate": raw.get("funding_rate"),
                    "spread_bps": raw.get("spread_bps"),
                    "slippage_bps": raw.get("slippage_bps"),
                    "liquidations": raw.get("liquidations"),
                    "basis_bps": raw.get("basis_bps"),
                }
                for index in range(periods)
            ]
    else:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    if "timestamp" not in frame.columns:
        return pd.DataFrame()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    for column in frame.columns:
        if column != "timestamp":
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return normalize_frame(frame)


def _config_dict(config: DiscoveryConfig) -> dict[str, Any]:
    return {
        name: list(value) if isinstance(value, tuple) else value
        for name, value in config.__dict__.items()
    }


def _candidate_matches(candidate: DiscoveryCandidate, wanted: set[str]) -> bool:
    identity = candidate.member.identity_key
    aliases = {
        candidate.member.symbol,
        candidate.member.canonical_asset_id.upper()
        if candidate.member.canonical_asset_id
        else None,
        candidate.member.contract_address.upper()
        if candidate.member.contract_address
        else None,
        identity.upper() if identity else None,
    }
    aliases.discard(None)
    return bool(aliases & wanted)


def _first_eligible_records(
    observations: list[dict[str, Any]], *, min_rank_score: float = 50.0
) -> dict[str, dict[str, Any]]:
    first: dict[str, dict[str, Any]] = {}
    for observation in observations:
        for candidate in observation.get("candidates", []):
            if not _candidate_eligible_at_threshold(candidate, min_rank_score):
                continue
            identity = identity_key_for(
                candidate.get("canonical_asset_id"), candidate.get("contract_address")
            )
            if not identity or identity in first:
                continue
            record = dict(candidate)
            record["first_eligible_at"] = observation["observation_timestamp"]
            first[identity] = record
    return first


def _candidate_eligible_at_threshold(candidate: dict[str, Any], min_rank_score: float) -> bool:
    """Apply the ranking and liquidity gates at one sensitivity threshold."""
    if candidate.get("liquidity", {}).get("state") != "PASS":
        return False
    if candidate.get("missingness"):
        return False
    score = candidate.get("rank_score")
    return (
        candidate.get("features", {}).get("return_7d") is not None
        and score is not None
        and float(score) >= min_rank_score
    )


def _eligible_count(observation: dict[str, Any], *, min_rank_score: float) -> int:
    return sum(
        _candidate_eligible_at_threshold(candidate, min_rank_score)
        for candidate in observation.get("candidates", [])
    )


def _member_from_record(record: dict[str, Any]) -> UniverseMember:
    return UniverseMember(
        symbol=str(record.get("symbol") or ""),
        canonical_asset_id=record.get("canonical_asset_id"),
        contract_address=record.get("contract_address"),
        venue=record.get("venue"),
        contract_symbol=record.get("contract_symbol"),
        quote_currency=record.get("quote_currency"),
        observation_timestamp=as_utc(record.get("observation_timestamp") or record.get("first_eligible_at")),
        source_timestamp=as_utc(record["source_timestamp"]) if record.get("source_timestamp") else None,
        available_at=as_utc(record["available_at"]) if record.get("available_at") else None,
        universe_source=str(record.get("universe_source") or "offline_fixture"),
        data_completeness=str(record.get("data_completeness") or "complete"),
        missingness_reason=record.get("missingness_reason"),
        rank=(
            int(record["universe_rank"])
            if record.get("universe_rank") is not None
            else int(record["rank"])
            if record.get("rank") is not None
            else None
        ),
        exchange_contract_type=str(record.get("exchange_contract_type") or "perpetual"),
    )


def _target_is_canonical_id(target: str, matches: tuple[UniverseMember, ...]) -> bool:
    return any(
        (member.canonical_asset_id and member.canonical_asset_id.upper() == target.strip().upper())
        or (member.contract_address and member.contract_address.upper() == target.strip().upper())
        for member in matches
    )


def _paper_setup_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "classification": payload["classification"],
        "valid": payload["valid"],
        "direction": payload.get("direction"),
        "entry_zone": payload.get("entry_zone"),
        "invalidation": payload.get("invalidation"),
        "targets": payload.get("targets") or [],
        "expected_move_pct": payload.get("expected_move_pct"),
        "net_rr": payload.get("net_rr"),
        "estimated_cost_pct": payload.get("estimated_cost_pct"),
        "no_chase": payload.get("no_chase"),
        "blockers": tuple(payload.get("blockers") or []),
        "data_complete": payload.get("data_complete", True),
        "slippage_bps": payload.get("slippage_bps"),
        "spread_bps": payload.get("spread_bps"),
    }


def _entry_from_zone(zone: list[float] | None, side: str) -> float | None:
    if not zone:
        return None
    return float(zone[-1] if side == "long" else zone[0])


def _no_chase(price: Any, entry: float | None, side: str, config: DiscoveryConfig) -> bool | None:
    return no_chase_allowed(price, entry, side, config.retest_distance_pct)


def no_chase_allowed(
    price: Any, entry: float | None, side: str, max_distance_pct: float
) -> bool | None:
    """Return whether the observed price remains within the valid entry zone."""
    if price is None or entry is None or entry <= 0:
        return None
    price = float(price)
    if side == "long":
        return price <= entry * (1 + max_distance_pct)
    return price >= entry * (1 - max_distance_pct)


def _latest_funding(derivatives: pd.DataFrame | None, as_of: datetime) -> float | None:
    row = None if derivatives is None else normalize_frame(derivatives).loc[lambda value: value.index <= pd.Timestamp(as_utc(as_of))]
    if row is None or row.empty or "funding_rate" not in row:
        return None
    value = row["funding_rate"].iloc[-1]
    return None if pd.isna(value) else float(value)


def _cost_pct(
    config: DiscoveryConfig,
    funding_rate: float | None,
    holding_intervals: float,
    *,
    slippage_bps: float | None = None,
    spread_bps: float | None = None,
) -> float:
    effective_slippage = (
        config.default_slippage_bps if slippage_bps is None else float(slippage_bps)
    )
    effective_spread = 0.0 if spread_bps is None else float(spread_bps)
    return (
        2 * (config.fee_bps_per_side + effective_slippage) / 10_000
        + effective_spread / 10_000
        + abs(funding_rate or 0.0) * holding_intervals
    )


def _net_rr(
    entry: float | None,
    invalidation: float | None,
    tp2: float | None,
    funding_rate: float | None,
    config: DiscoveryConfig,
    *,
    slippage_bps: float | None = None,
    spread_bps: float | None = None,
) -> float | None:
    if entry is None or invalidation is None or tp2 is None:
        return None
    risk = abs(entry - invalidation) / entry
    reward = abs(tp2 - entry) / entry
    cost = _cost_pct(
        config,
        funding_rate,
        1,
        slippage_bps=slippage_bps,
        spread_bps=spread_bps,
    )
    return (reward - cost) / (risk + cost) if risk + cost > 0 else None


def _regime(return_7d: Any) -> str:
    if return_7d is None:
        return "UNKNOWN"
    value = float(return_7d)
    if value >= 0.05:
        return "BULL"
    if value <= -0.05:
        return "BEAR"
    return "RANGE"


def _mean_field(rows: list[dict[str, Any]], field_name: str) -> float | None:
    values = [float(row[field_name]) for row in rows if row.get(field_name) is not None]
    return round(float(np.mean(values)), 6) if values else None


def _mean_detection_latency(rows: list[dict[str, Any]], *, window_start: datetime | None) -> float:
    if window_start is None:
        return 0.0
    values = []
    for row in rows:
        try:
            values.append(
                (as_utc(row["first_eligible_at"]) - as_utc(window_start)).total_seconds() / 3600
            )
        except (KeyError, TypeError, ValueError):
            continue
    return float(np.mean(values)) if values else 0.0
