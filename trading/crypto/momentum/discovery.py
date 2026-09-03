"""Research-only crypto momentum discovery and liquidity ranking.

The ranking layer produces observations and inferences for human review.  It
does not create signals for execution, live alerts, or trading instructions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd

from trading.crypto.momentum.universe import UniverseMember, as_utc


DISCOVERY_HYPOTHESIS = (
    "At each historical observation time, a point-in-time universe composed of the top 100 "
    "crypto assets by available market capitalization plus sufficiently liquid perpetual "
    "futures can rank emerging relative-strength and momentum leaders early enough to "
    "produce a realistic, risk-controlled paper-trading setup after fees, funding, and slippage."
)


@dataclass(frozen=True)
class DiscoveryConfig:
    """All ranking, liquidity, outcome, and paper-cost thresholds in one contract."""

    min_quote_volume_24h: float = 5_000_000.0
    min_open_interest: float = 1_000_000.0
    require_open_interest: bool = True
    max_spread_bps: float = 20.0
    max_slippage_bps: float = 25.0
    min_history_hours: int = 72
    max_price_staleness_hours: int = 8
    breakout_lookback_bars: int = 42
    trend_persistence_bars: int = 24
    volume_baseline_bars: int = 168
    retest_distance_pct: float = 0.02
    min_rank_score: float = 50.0
    universe_benchmark: str = "median_7d_return"
    meaningful_move_pct: float = 0.10
    outcome_horizon_hours: int = 168
    fee_bps_per_side: float = 5.0
    default_slippage_bps: float = 10.0
    funding_interval_hours: int = 8
    cadence_hours: int = 6
    sensitivity_cadence_hours: tuple[int, ...] = (3, 6, 12)
    sensitivity_score_thresholds: tuple[float, ...] = (50.0, 60.0, 70.0)
    score_weights: dict[str, float] = field(
        default_factory=lambda: {
            "return_7d": 0.25,
            "return_24h": 0.15,
            "relative_strength": 0.25,
            "acceleration": 0.15,
            "breakout": 0.10,
            "volume_expansion": 0.05,
            "trend_persistence": 0.05,
        }
    )


def load_discovery_config(path: str | Path | None = None) -> DiscoveryConfig:
    """Load the discovery config from JSON, preserving documented defaults."""
    config_path = Path(path) if path is not None else Path(__file__).with_name("discovery_defaults.json")
    payload = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    defaults = DiscoveryConfig()
    values: dict[str, Any] = {}
    for name in (
        "min_quote_volume_24h",
        "min_open_interest",
        "require_open_interest",
        "max_spread_bps",
        "max_slippage_bps",
        "min_history_hours",
        "max_price_staleness_hours",
        "breakout_lookback_bars",
        "trend_persistence_bars",
        "volume_baseline_bars",
        "retest_distance_pct",
        "min_rank_score",
        "universe_benchmark",
        "meaningful_move_pct",
        "outcome_horizon_hours",
        "fee_bps_per_side",
        "default_slippage_bps",
        "funding_interval_hours",
        "cadence_hours",
    ):
        values[name] = payload.get(name, getattr(defaults, name))
    values["sensitivity_cadence_hours"] = tuple(
        int(value) for value in payload.get("sensitivity_cadence_hours", defaults.sensitivity_cadence_hours)
    )
    values["sensitivity_score_thresholds"] = tuple(
        float(value)
        for value in payload.get("sensitivity_score_thresholds", defaults.sensitivity_score_thresholds)
    )
    values["score_weights"] = {
        **defaults.score_weights,
        **(payload.get("score_weights") if isinstance(payload.get("score_weights"), dict) else {}),
    }
    return DiscoveryConfig(**values)


@dataclass(frozen=True)
class AssetMarketData:
    """Normalized historical candles and derivatives observations for one identity."""

    frames: dict[str, pd.DataFrame]
    derivatives: pd.DataFrame | None = None
    source: str = "unknown"
    source_timestamp: datetime | None = None

    def frame(self, timeframe: str) -> pd.DataFrame:
        return normalize_frame(self.frames.get(timeframe, pd.DataFrame()))


class MarketDataProvider(Protocol):
    def data_for(self, member: UniverseMember) -> AssetMarketData | None:
        """Return data keyed by canonical identity, or None if unavailable."""


@dataclass(frozen=True)
class LiquidityAssessment:
    state: str
    quote_volume_24h: float | None
    open_interest: float | None
    spread_bps: float | None
    slippage_bps: float | None
    history_hours: float | None
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "quote_volume_24h": self.quote_volume_24h,
            "open_interest": self.open_interest,
            "spread_bps": self.spread_bps,
            "slippage_bps": self.slippage_bps,
            "history_hours": self.history_hours,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class DiscoveryCandidate:
    member: UniverseMember
    observation_timestamp: datetime
    rank_score: float | None
    ranking_state: str
    features: dict[str, Any]
    liquidity: LiquidityAssessment
    evidence: dict[str, list[str]]
    missingness: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        return self.ranking_state == "ELIGIBLE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.member.symbol,
            "canonical_asset_id": self.member.canonical_asset_id,
            "contract_address": self.member.contract_address,
            "venue": self.member.venue,
            "contract_symbol": self.member.contract_symbol,
            "quote_currency": self.member.quote_currency,
            "observation_timestamp": self.observation_timestamp.isoformat(),
            "source_timestamp": self.member.source_timestamp.isoformat()
            if self.member.source_timestamp
            else None,
            "available_at": self.member.available_at.isoformat()
            if self.member.available_at
            else None,
            "universe_source": self.member.universe_source,
            "universe_rank": self.member.rank,
            "exchange_contract_type": self.member.exchange_contract_type,
            "data_completeness": self.member.data_completeness,
            "missingness_reason": self.member.missingness_reason,
            "rank_score": round(self.rank_score, 4) if self.rank_score is not None else None,
            "ranking_state": self.ranking_state,
            "features": self.features,
            "liquidity": self.liquidity.to_dict(),
            "evidence": self.evidence,
            "missingness": list(self.missingness),
        }


def normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize timestamped OHLCV or derivative rows without adding future data."""
    if frame is None or frame.empty:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC"))
    result = frame.copy()
    if "timestamp" in result.columns:
        result["timestamp"] = pd.to_datetime(result["timestamp"], utc=True)
        result = result.set_index("timestamp")
    elif not isinstance(result.index, pd.DatetimeIndex):
        return pd.DataFrame()
    else:
        result.index = pd.to_datetime(result.index, utc=True)
    return result.sort_index().loc[~result.index.duplicated(keep="last")]


def rank_universe(
    members: tuple[UniverseMember, ...],
    data_by_identity: dict[str, AssetMarketData],
    observation_timestamp: datetime,
    *,
    config: DiscoveryConfig | None = None,
) -> list[DiscoveryCandidate]:
    """Calculate point-in-time features and rank all observed members."""
    cfg = config or load_discovery_config()
    as_of = as_utc(observation_timestamp)
    normalized_data = {
        key: _normalize_market_data(value) for key, value in data_by_identity.items()
    }
    benchmark_return = _universe_benchmark_return(members, normalized_data, as_of, cfg)
    benchmark_returns = {
        "BTC": _asset_return_for_symbol("BTC", members, normalized_data, as_of, 168, cfg),
        "ETH": _asset_return_for_symbol("ETH", members, normalized_data, as_of, 168, cfg),
    }
    candidates: list[DiscoveryCandidate] = []
    for member in members:
        key = member.identity_key
        market = normalized_data.get(key) if key else None
        if market is None:
            candidates.append(
                _unavailable_candidate(member, as_of, "market_data_provider_unavailable")
            )
            continue
        if market.source_timestamp is not None and market.source_timestamp > as_of:
            candidates.append(
                _unavailable_candidate(member, as_of, "market_data_source_timestamp_after_observation")
            )
            continue
        features, missing = calculate_features(
            member,
            market,
            as_of,
            benchmark_return=benchmark_return,
            benchmark_returns=benchmark_returns,
            config=cfg,
        )
        liquidity = assess_liquidity(member, market, as_of, cfg)
        required_missing = list(missing)
        if member.data_completeness.lower() not in {"complete", "full"}:
            required_missing.append(
                member.missingness_reason or "universe_member_data_incomplete"
            )
        if liquidity.state == "UNKNOWN":
            required_missing.extend(liquidity.reasons)
        rank_score = features.get("rank_score")
        if required_missing:
            state = "UNKNOWN"
        elif liquidity.state == "REJECT":
            state = "REJECTED_LIQUIDITY"
        elif (features.get("return_7d") is None) or (
            rank_score is None or float(rank_score) < cfg.min_rank_score
        ):
            state = "BELOW_RANK_THRESHOLD"
        else:
            state = "ELIGIBLE"
        candidates.append(
            DiscoveryCandidate(
                member=member,
                observation_timestamp=as_of,
                rank_score=features.pop("rank_score", None),
                ranking_state=state,
                features=features,
                liquidity=liquidity,
                evidence={
                    "facts": _feature_facts(member, features, liquidity),
                    "inferences": (
                        ["candidate_ranked_by_configured_momentum_composite"]
                        if rank_score is not None
                        else []
                    ),
                    "hypotheses": [DISCOVERY_HYPOTHESIS],
                    "unknowns": required_missing,
                },
                missingness=tuple(required_missing),
            )
        )
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.rank_score is not None,
            candidate.rank_score if candidate.rank_score is not None else -1.0,
            candidate.member.symbol,
            candidate.member.canonical_asset_id or "",
        ),
        reverse=True,
    )


def calculate_features(
    member: UniverseMember,
    market: AssetMarketData,
    as_of: datetime,
    *,
    benchmark_return: float | None,
    benchmark_returns: dict[str, float | None],
    config: DiscoveryConfig,
) -> tuple[dict[str, Any], list[str]]:
    """Calculate only information timestamped at or before ``as_of``."""
    del member  # identity is carried by the returned candidate, not a feature.
    timestamp = as_utc(as_of)
    one_hour = market.frame("1h")
    four_hour = market.frame("4h")
    missing: list[str] = []
    returns: dict[str, float | None] = {}
    for name, hours in (("1h", 1), ("4h", 4), ("24h", 24), ("3d", 72), ("7d", 168)):
        returns[name] = price_return(one_hour, timestamp, hours, config)
        if returns[name] is None:
            missing.append(f"return_{name}")
    current, current_ts = close_at(one_hour, timestamp)
    if current is None:
        missing.append("current_price")
    previous_24h = price_return(one_hour, timestamp - pd.Timedelta(hours=24), 24, config)
    acceleration = (
        returns["24h"] - previous_24h
        if returns["24h"] is not None and previous_24h is not None
        else None
    )
    if acceleration is None:
        missing.append("momentum_acceleration")
    breakout_pct = _breakout_pct(one_hour, timestamp, current, config)
    volume_expansion = _volume_expansion(one_hour, timestamp, config)
    trend_persistence = _trend_persistence(one_hour, timestamp, config)
    for name, value in (
        ("breakout_pct", breakout_pct),
        ("volume_expansion", volume_expansion),
        ("trend_persistence", trend_persistence),
    ):
        if value is None:
            missing.append(name)
    structure_1h = structure_state(one_hour, timestamp, bars=6)
    structure_4h = structure_state(four_hour, timestamp, bars=6)
    if structure_1h == "INSUFFICIENT_DATA":
        missing.append("structure_1h")
    if structure_4h == "INSUFFICIENT_DATA":
        missing.append("structure_4h")
    atr_pct = atr_percent(four_hour, timestamp, window=14)
    if atr_pct is None:
        missing.append("atr_4h")
    distance_from_swing_high = _distance_from_swing_high(one_hour, timestamp, current, config)
    pullback_status = _pullback_status(distance_from_swing_high, config)
    relative_strength = {
        "BTC": _relative(returns["7d"], benchmark_returns.get("BTC")),
        "ETH": _relative(returns["7d"], benchmark_returns.get("ETH")),
        "universe": _relative(returns["7d"], benchmark_return),
    }
    derivatives = _derivative_features(market.derivatives, timestamp)
    if any(value is None for value in relative_strength.values()):
        missing.append("relative_strength")
    score_components = {
        "return_7d": _bounded_score(returns["7d"], 0.35),
        "return_24h": _bounded_score(returns["24h"], 0.10),
        "relative_strength": _bounded_score(relative_strength["universe"], 0.20),
        "acceleration": _bounded_score(acceleration, 0.10),
        "breakout": _bounded_score(breakout_pct, 0.05),
        "volume_expansion": _bounded_score(
            None if volume_expansion is None else volume_expansion - 1.0, 1.0
        ),
        "trend_persistence": _bounded_score(
            None if trend_persistence is None else trend_persistence - 0.5, 0.5
        ),
    }
    score = None
    if not any(value is None for value in score_components.values()):
        score = sum(config.score_weights.get(key, 0.0) * float(value) for key, value in score_components.items())
    features: dict[str, Any] = {
        "return_1h": returns["1h"],
        "return_4h": returns["4h"],
        "return_24h": returns["24h"],
        "return_3d": returns["3d"],
        "return_7d": returns["7d"],
        "relative_strength": relative_strength,
        "universe_benchmark_return": benchmark_return,
        "momentum_acceleration": acceleration,
        "breakout_pct": breakout_pct,
        "volume_expansion": volume_expansion,
        "trend_persistence": trend_persistence,
        "structure_1h": structure_1h,
        "structure_4h": structure_4h,
        "higher_high_higher_low": structure_1h == "HIGHER_HIGH_HIGHER_LOW"
        and structure_4h == "HIGHER_HIGH_HIGHER_LOW",
        "distance_from_swing_high": distance_from_swing_high,
        "pullback_retest_status": pullback_status,
        "atr_4h_pct": atr_pct,
        "derivatives": derivatives,
        "price": current,
        "data_timestamp": current_ts.isoformat() if current_ts is not None else None,
        "data_source": market.source,
        "data_source_timestamp": market.source_timestamp.isoformat()
        if market.source_timestamp
        else None,
        "score_components": score_components,
        "rank_score": score,
    }
    return features, sorted(set(missing))


def assess_liquidity(
    member: UniverseMember,
    market: AssetMarketData,
    as_of: datetime,
    config: DiscoveryConfig,
) -> LiquidityAssessment:
    """Apply explicit derivative liquidity constraints without favorable imputation."""
    derivatives = normalize_frame(market.derivatives if market.derivatives is not None else pd.DataFrame())
    timestamp = as_utc(as_of)
    row = _latest_row(derivatives, timestamp, max_age_hours=24)
    one_hour = market.frame("1h")
    _, current_ts = close_at(one_hour, timestamp)
    history_hours = None
    if current_ts is not None and not one_hour.empty:
        history_hours = max(0.0, (current_ts - one_hour.index[0]).total_seconds() / 3600.0)
    if member.exchange_contract_type != "perpetual" or not member.contract_symbol:
        return LiquidityAssessment(
            "REJECT",
            None,
            None,
            None,
            None,
            history_hours,
            ("no_perpetual_contract_metadata",),
        )
    if row is None:
        return LiquidityAssessment(
            "UNKNOWN",
            None,
            None,
            None,
            None,
            history_hours,
            ("derivatives_data_unavailable_or_stale",),
        )
    quote_volume = _numeric(row, "quote_volume_24h")
    open_interest = _numeric(row, "open_interest")
    spread_bps = _numeric(row, "spread_bps")
    slippage_bps = _numeric(row, "slippage_bps")
    missing = [
        name
        for name, value in (
            ("quote_volume_24h", quote_volume),
            ("spread_bps", spread_bps),
            ("slippage_bps", slippage_bps),
        )
        if value is None
    ]
    if config.require_open_interest and open_interest is None:
        missing.append("open_interest")
    if history_hours is None or history_hours < config.min_history_hours:
        missing.append("minimum_trading_history")
    if missing:
        return LiquidityAssessment(
            "UNKNOWN", quote_volume, open_interest, spread_bps, slippage_bps, history_hours, tuple(missing)
        )
    rejected: list[str] = []
    if quote_volume < config.min_quote_volume_24h:
        rejected.append("quote_volume_below_minimum")
    if config.require_open_interest and open_interest < config.min_open_interest:
        rejected.append("open_interest_below_minimum")
    if spread_bps > config.max_spread_bps:
        rejected.append("spread_above_maximum")
    if slippage_bps > config.max_slippage_bps:
        rejected.append("slippage_above_maximum")
    return LiquidityAssessment(
        "REJECT" if rejected else "PASS",
        quote_volume,
        open_interest,
        spread_bps,
        slippage_bps,
        history_hours,
        tuple(rejected),
    )


def price_return(
    frame: pd.DataFrame, as_of: datetime, hours: int, config: DiscoveryConfig | None = None
) -> float | None:
    cfg = config or DiscoveryConfig()
    current, current_ts = close_at(frame, as_of)
    prior, prior_ts = close_at(frame, as_utc(as_of) - pd.Timedelta(hours=hours))
    if current is None or prior is None or current <= 0 or prior <= 0:
        return None
    if current_ts is None or (as_utc(as_of) - current_ts).total_seconds() / 3600 > cfg.max_price_staleness_hours:
        return None
    target = as_utc(as_of) - pd.Timedelta(hours=hours)
    if prior_ts is None or (target - prior_ts.to_pydatetime()).total_seconds() / 3600 > cfg.max_price_staleness_hours:
        return None
    return float(current / prior - 1.0)


def close_at(frame: pd.DataFrame, as_of: datetime) -> tuple[float | None, pd.Timestamp | None]:
    row = _latest_row(normalize_frame(frame), as_utc(as_of))
    if row is None or "close" not in row:
        return None, None
    try:
        return float(row["close"]), row.name
    except (TypeError, ValueError):
        return None, None


def structure_state(frame: pd.DataFrame, as_of: datetime, *, bars: int) -> str:
    limited = normalize_frame(frame).loc[lambda value: value.index <= pd.Timestamp(as_utc(as_of))].tail(bars)
    if len(limited) < bars or not {"high", "low"}.issubset(limited.columns):
        return "INSUFFICIENT_DATA"
    highs = limited["high"].astype(float).to_numpy()
    lows = limited["low"].astype(float).to_numpy()
    higher = bool(np.all(np.diff(highs) > 0) and np.all(np.diff(lows) > 0))
    lower = bool(np.all(np.diff(highs) < 0) and np.all(np.diff(lows) < 0))
    if higher:
        return "HIGHER_HIGH_HIGHER_LOW"
    if lower:
        return "LOWER_HIGH_LOWER_LOW"
    return "MIXED"


def atr_percent(frame: pd.DataFrame, as_of: datetime, *, window: int) -> float | None:
    limited = normalize_frame(frame).loc[lambda value: value.index <= pd.Timestamp(as_utc(as_of))].tail(window + 1)
    if len(limited) < window or not {"high", "low", "close"}.issubset(limited.columns):
        return None
    high = limited["high"].astype(float)
    low = limited["low"].astype(float)
    close = limited["close"].astype(float)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1).dropna()
    if true_range.empty or close.iloc[-1] <= 0:
        return None
    return float(true_range.tail(window).mean() / close.iloc[-1])


def _normalize_market_data(market: AssetMarketData) -> AssetMarketData:
    return AssetMarketData(
        frames={key: normalize_frame(value) for key, value in market.frames.items()},
        derivatives=normalize_frame(market.derivatives) if market.derivatives is not None else None,
        source=market.source,
        source_timestamp=market.source_timestamp,
    )


def _latest_row(frame: pd.DataFrame, as_of: datetime, max_age_hours: int | None = None) -> pd.Series | None:
    if frame.empty:
        return None
    timestamp = pd.Timestamp(as_utc(as_of))
    limited = frame.loc[frame.index <= timestamp]
    if limited.empty:
        return None
    row = limited.iloc[-1]
    if max_age_hours is not None and (timestamp - row.name).total_seconds() / 3600 > max_age_hours:
        return None
    return row


def _numeric(row: pd.Series, field_name: str) -> float | None:
    if field_name not in row.index or pd.isna(row[field_name]):
        return None
    try:
        return float(row[field_name])
    except (TypeError, ValueError):
        return None


def _relative(value: float | None, benchmark: float | None) -> float | None:
    return value - benchmark if value is not None and benchmark is not None else None


def _bounded_score(value: float | None, scale: float) -> float | None:
    if value is None or scale <= 0:
        return None
    return float(max(0.0, min(100.0, 50.0 + (value / scale) * 50.0)))


def _breakout_pct(frame: pd.DataFrame, as_of: datetime, current: float | None, config: DiscoveryConfig) -> float | None:
    if current is None:
        return None
    limited = normalize_frame(frame).loc[lambda value: value.index <= pd.Timestamp(as_utc(as_of))]
    if len(limited) <= config.breakout_lookback_bars:
        return None
    previous = limited.iloc[-config.breakout_lookback_bars - 1 : -1]
    if "high" not in previous:
        return None
    high = float(previous["high"].astype(float).max())
    return current / high - 1.0 if high > 0 else None


def _volume_expansion(frame: pd.DataFrame, as_of: datetime, config: DiscoveryConfig) -> float | None:
    limited = normalize_frame(frame).loc[lambda value: value.index <= pd.Timestamp(as_utc(as_of))]
    if len(limited) < config.volume_baseline_bars or "volume" not in limited:
        return None
    recent = float(limited["volume"].tail(24).astype(float).mean())
    baseline = float(limited["volume"].tail(config.volume_baseline_bars).astype(float).mean())
    return recent / baseline if baseline > 0 else None


def _trend_persistence(frame: pd.DataFrame, as_of: datetime, config: DiscoveryConfig) -> float | None:
    limited = normalize_frame(frame).loc[lambda value: value.index <= pd.Timestamp(as_utc(as_of))]
    if len(limited) < config.trend_persistence_bars + 1 or "close" not in limited:
        return None
    changes = limited["close"].astype(float).diff().tail(config.trend_persistence_bars).dropna()
    return float((changes > 0).mean()) if len(changes) else None


def _distance_from_swing_high(
    frame: pd.DataFrame, as_of: datetime, current: float | None, config: DiscoveryConfig
) -> float | None:
    if current is None:
        return None
    limited = normalize_frame(frame).loc[lambda value: value.index <= pd.Timestamp(as_utc(as_of))]
    if len(limited) < config.breakout_lookback_bars or "high" not in limited:
        return None
    high = float(limited["high"].tail(config.breakout_lookback_bars).astype(float).max())
    return current / high - 1.0 if high > 0 else None


def _derivative_features(derivatives: pd.DataFrame | None, as_of: datetime) -> dict[str, float | None]:
    frame = normalize_frame(derivatives if derivatives is not None else pd.DataFrame())
    latest = _latest_row(frame, as_of, max_age_hours=24)
    if latest is None:
        return {
            "open_interest_change_24h": None,
            "funding_rate": None,
            "liquidations_24h": None,
            "contract_volume_24h": None,
            "basis_bps": None,
            "spread_bps": None,
            "slippage_bps": None,
        }
    previous = _latest_row(frame, as_of=as_utc(as_of) - pd.Timedelta(hours=24), max_age_hours=32)
    latest_oi = _numeric(latest, "open_interest")
    previous_oi = _numeric(previous, "open_interest") if previous is not None else None
    oi_change = (
        latest_oi / previous_oi - 1.0
        if latest_oi is not None and previous_oi not in (None, 0)
        else None
    )
    funding = _numeric(latest, "funding_rate")
    liquidations = None
    if "liquidations" in frame.columns:
        recent = frame.loc[
            (frame.index > pd.Timestamp(as_utc(as_of)) - pd.Timedelta(hours=24))
            & (frame.index <= pd.Timestamp(as_utc(as_of)))
        ]
        if not recent.empty:
            liquidations = float(recent["liquidations"].astype(float).sum())
    return {
        "open_interest_change_24h": oi_change,
        "funding_rate": funding,
        "liquidations_24h": liquidations,
        "contract_volume_24h": _numeric(latest, "quote_volume_24h"),
        "basis_bps": _numeric(latest, "basis_bps"),
        "spread_bps": _numeric(latest, "spread_bps"),
        "slippage_bps": _numeric(latest, "slippage_bps"),
    }


def _pullback_status(distance: float | None, config: DiscoveryConfig) -> str:
    if distance is None:
        return "UNKNOWN"
    if abs(distance) <= config.retest_distance_pct:
        return "RETEST_ZONE"
    if distance > config.retest_distance_pct:
        return "ABOVE_RECENT_SWING"
    return "PULLBACK_FROM_SWING"


def _universe_benchmark_return(
    members: tuple[UniverseMember, ...],
    data: dict[str, AssetMarketData],
    as_of: datetime,
    config: DiscoveryConfig,
) -> float | None:
    values = []
    for member in members:
        if not member.identity_key:
            continue
        value = price_return(data.get(member.identity_key, AssetMarketData({})).frame("1h"), as_of, 168, config)
        if value is not None:
            values.append(value)
    if not values:
        return None
    if config.universe_benchmark == "mean_7d_return":
        return float(np.mean(values))
    if config.universe_benchmark != "median_7d_return":
        raise ValueError(
            "universe_benchmark must be 'median_7d_return' or 'mean_7d_return'"
        )
    return float(np.median(values))


def _asset_return_for_symbol(
    symbol: str,
    members: tuple[UniverseMember, ...],
    data: dict[str, AssetMarketData],
    as_of: datetime,
    hours: int,
    config: DiscoveryConfig,
) -> float | None:
    matches = [member for member in members if member.symbol == symbol and member.identity_key]
    if not matches:
        return None
    values = [
        price_return(data[member.identity_key].frame("1h"), as_of, hours, config)
        for member in matches
        if member.identity_key in data
    ]
    values = [value for value in values if value is not None]
    return float(values[0]) if values else None


def _unavailable_candidate(member: UniverseMember, as_of: datetime, reason: str) -> DiscoveryCandidate:
    liquidity = LiquidityAssessment("UNKNOWN", None, None, None, None, None, (reason,))
    return DiscoveryCandidate(
        member=member,
        observation_timestamp=as_of,
        rank_score=None,
        ranking_state="UNKNOWN",
        features={},
        liquidity=liquidity,
        evidence={
            "facts": ["universe_membership_observed"],
            "inferences": [],
            "hypotheses": [DISCOVERY_HYPOTHESIS],
            "unknowns": [reason],
        },
        missingness=(reason,),
    )


def _feature_facts(
    member: UniverseMember, features: dict[str, Any], liquidity: LiquidityAssessment
) -> list[str]:
    facts = [
        f"observed_symbol={member.symbol}",
        f"universe_source={member.universe_source}",
        f"return_7d={features.get('return_7d')}",
        f"relative_strength_universe={features.get('relative_strength', {}).get('universe')}",
        f"liquidity_state={liquidity.state}",
    ]
    return facts
