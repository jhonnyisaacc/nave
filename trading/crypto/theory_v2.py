"""
Theory v2 execution engine — top-down weekly→daily→4H→1H pipeline.

This module is the live-execution counterpart to
``scripts/theory_backtest.py``. The base pipeline was validated on ~9.5
years of BTC/ETH history (see ``docs/analysis/btc_eth_historical_review.md``,
+0.50R/+0.33R per trade for BTC/ETH at fixed 2R targeting). On top of the
base pipeline, this module enforces the four refinement rules added during
the iter 3–6 theory loop, which the backtest engine never implemented:

  - **iter 4** post-climax cooldown (single-candle regime inversions)
  - **iter 5** impulse-leg identification + chase prevention
  - **iter 6** ATR-based minimum stop-loss distance
  - iter 3 re-entry discipline is **implicitly** covered by iter 5: any
    fresh entry must be in the deep retracement zone of a leg, which by
    construction requires price to have left the prior entry zone.

Pipeline (each gate must pass before the next is checked):

    weekly momentum    → velocity over 4 weekly bars must exceed 1.2 weekly
                         ATRs (iter 13 high-momentum regime, iter 14 tuned)
    daily confirmation → trend over 20 daily closes must match bias
    post-climax gate   → no daily TR > 3× 20-day ATR within last 10 days
    chase gate         → price inside 50–95% retracement of latest
                         daily impulse leg
    4H setup           → trend over 12 4H closes must match bias
    1H entry           → swing of last 24 1H bars OR 1.5× daily ATR floor
                         (whichever is wider) defines stop;
                         take-profit is fixed 2R

Each gate produces a ``TheoryV2Decision`` with the stage at which the
evaluation stopped, so the live analyzer can explain *why* a coin is on
the sidelines, not just that it is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import pandas as pd

from trading.crypto.cot.cot_gate import weekly_cot_filter
from trading.crypto.signals import Direction, Signal, Timeframe

if TYPE_CHECKING:
    from trading.crypto.analysis.squeeze_daily import SqueezeDailyState, SqueezeConfig

CotHistoryFn = Callable[[str, pd.Timestamp], "pd.DataFrame | None"]

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Pure helpers (ported verbatim from scripts/theory_backtest.py)
# --------------------------------------------------------------------------- #


def trend(series: pd.Series, window: int, deadband: float = 0.005) -> str:
    """Classify the latest close vs its rolling mean.

    Returns one of ``"long"``, ``"short"``, ``"neutral"``. The deadband
    around the mean prevents flipping on tiny noise.
    """
    if len(series) < window + 1:
        return "neutral"
    ma = series.rolling(window).mean()
    last = series.iloc[-1]
    ma_now = ma.iloc[-1]
    if pd.isna(ma_now):
        return "neutral"
    if last > ma_now * (1 + deadband):
        return "long"
    if last < ma_now * (1 - deadband):
        return "short"
    return "neutral"


def weekly_bias(weekly: pd.DataFrame) -> str:
    if weekly.empty:
        return "neutral"
    return trend(weekly["close"], window=8, deadband=0.04)


def _true_range(df: pd.DataFrame) -> pd.Series:
    """Per-bar true range for an OHLC frame."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def weekly_atr(weekly: pd.DataFrame, window: int = 8) -> float | None:
    """Weekly ATR over the last ``window`` weekly bars."""
    if len(weekly) < window + 1:
        return None
    tr = _true_range(weekly)
    atr = float(tr.tail(window).mean())
    return atr if atr > 0 else None


def momentum_bias(
    weekly: pd.DataFrame,
    lookback: int = 4,
    min_velocity: float = 1.2,
    atr_window: int = 8,
) -> tuple[str, float | None]:
    """High-momentum bias: price must move fast relative to its own volatility.

    Velocity = (close_now − close_{lookback bars ago}) / weekly ATR.

    A positive velocity above ``min_velocity`` means price rose more than
    ``min_velocity`` weekly ATRs in ``lookback`` weeks → high-momentum up-move.
    The symmetric rule fires shorts. Slow grinds (low displacement) and chop
    (alternating bars that cancel out) yield velocity near zero → neutral.

    Returns ``(bias, velocity)`` so the engine can surface the score in its
    decision metadata.
    """
    if weekly.empty or len(weekly) < max(lookback + 1, atr_window + 1):
        return "neutral", None
    atr = weekly_atr(weekly, atr_window)
    if atr is None:
        return "neutral", None
    close = weekly["close"].astype(float)
    displacement = float(close.iloc[-1] - close.iloc[-lookback - 1])
    velocity = displacement / atr
    if velocity > min_velocity:
        return "long", velocity
    if velocity < -min_velocity:
        return "short", velocity
    return "neutral", velocity


def range_breakout_bias(
    weekly: pd.DataFrame,
    range_window: int = 8,
    max_range_atrs: float = 1.5,
    breakout_buffer_atrs: float = 0.5,
    atr_window: int = 8,
) -> tuple[str, dict | None]:
    """Range-breakout fallback bias — iter 18 addition for iter 16 blind spot.

    Looks for a flat consolidation (high-low range of the last
    ``range_window - 1`` prior bars below ``max_range_atrs`` × weekly ATR),
    then fires when the latest weekly close breaks outside that range
    by at least ``breakout_buffer_atrs`` × ATR.

    Returns ``(bias, diagnostic)`` where ``diagnostic`` is a dict with
    ``range_high``, ``range_low``, ``range_size_atrs``, ``breakout_distance_atrs``
    — useful for surfacing in the decision metadata.
    """
    if weekly.empty or len(weekly) < max(range_window + 1, atr_window + 1):
        return "neutral", None
    atr = weekly_atr(weekly, atr_window)
    if atr is None:
        return "neutral", None

    close = weekly["close"].astype(float)
    # Prior-range window: close_{-range_window..-2} (exclude current bar)
    prior = close.iloc[-range_window:-1]
    if prior.empty:
        return "neutral", None
    range_high = float(prior.max())
    range_low = float(prior.min())
    range_size = range_high - range_low
    range_size_atrs = range_size / atr

    if range_size_atrs > max_range_atrs:
        return "neutral", {
            "range_high": range_high,
            "range_low": range_low,
            "range_size_atrs": range_size_atrs,
            "reason": "prior window not flat enough",
        }

    last_close = float(close.iloc[-1])
    buffer = breakout_buffer_atrs * atr
    diag = {
        "range_high": range_high,
        "range_low": range_low,
        "range_size_atrs": range_size_atrs,
    }
    if last_close > range_high + buffer:
        diag["breakout_distance_atrs"] = (last_close - range_high) / atr
        return "long", diag
    if last_close < range_low - buffer:
        diag["breakout_distance_atrs"] = (range_low - last_close) / atr
        return "short", diag
    diag["reason"] = "flat range but no breakout yet"
    return "neutral", diag


def daily_confirms(daily: pd.DataFrame, bias: str) -> bool:
    if daily.empty or bias == "neutral":
        return False
    return trend(daily["close"], window=10) == bias


def four_h_setup_valid(h4: pd.DataFrame, bias: str) -> bool:
    if h4.empty or bias == "neutral":
        return False
    return trend(h4["close"], window=8) == bias


def daily_atr(daily: pd.DataFrame, window: int = 14) -> float | None:
    """True-range average over the last ``window`` daily bars.

    Returns ``None`` when there are not enough bars.
    """
    if len(daily) < window + 1:
        return None
    high = daily["high"].astype(float)
    low = daily["low"].astype(float)
    close = daily["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return float(tr.tail(window).mean())


def _find_swing_targets(
    daily: pd.DataFrame,
    bias: str,
    entry: float,
    lookback: int = 60,
    swing_window: int = 5,
) -> list[float]:
    """Find structural swing-level targets from daily data.

    For longs: recent swing highs above entry.
    For shorts: recent swing lows below entry.
    Returns sorted list of target prices (nearest first).
    """
    if daily.empty or len(daily) < 2 * swing_window + 1:
        return []
    sub = daily.tail(lookback).reset_index(drop=True)
    highs = sub["high"].astype(float).values
    lows = sub["low"].astype(float).values

    targets: list[float] = []
    for i in range(swing_window, len(sub) - swing_window):
        if bias == "long":
            win_h = highs[i - swing_window : i + swing_window + 1]
            if highs[i] == win_h.max() and (win_h == highs[i]).sum() == 1:
                if float(highs[i]) > entry:
                    targets.append(float(highs[i]))
        else:
            win_l = lows[i - swing_window : i + swing_window + 1]
            if lows[i] == win_l.min() and (win_l == lows[i]).sum() == 1:
                if float(lows[i]) < entry:
                    targets.append(float(lows[i]))

    if bias == "long":
        return sorted(set(targets))
    return sorted(set(targets), reverse=True)


def one_h_entry(
    h1: pd.DataFrame,
    bias: str,
    daily: pd.DataFrame | None = None,
    atr_floor_mult: float = 1.5,
) -> tuple[float, float, list[float]] | None:
    """Compute (entry, stop_loss, targets) for the 1H trigger.

    Stop is the **wider** of:
      - the swing high/low of the last 24 1H bars (structural)
      - ``atr_floor_mult × daily ATR-14`` (volatility floor, iter 6)

    Targets use ZC1/ZC2 dynamic targeting when daily structure is available:
      - ZC1: nearest daily swing level above/below entry, or 1.5R fallback
      - ZC2: next swing level, or 2.5R fallback
    Falls back to fixed 2R when no daily data is supplied.

    Returns ``None`` if the geometry is degenerate.
    """
    if h1.empty or bias == "neutral":
        return None
    last = h1.iloc[-1]
    entry = float(last["close"])
    window = h1.tail(24)
    if window.empty:
        return None
    hi = float(window["high"].max())
    lo = float(window["low"].min())

    structural_risk = entry - lo if bias == "long" else hi - entry
    if structural_risk <= 0:
        return None

    risk = structural_risk
    if daily is not None and not daily.empty:
        atr = daily_atr(daily)
        if atr is not None:
            floor = atr_floor_mult * atr
            risk = max(structural_risk, floor)

    if bias == "long":
        sl = entry - risk
    else:
        sl = entry + risk

    # ZC1/ZC2 dynamic targeting
    targets: list[float] = []
    if daily is not None and not daily.empty:
        swing_targets = _find_swing_targets(daily, bias, entry)
        # Filter targets that give at least 1R reward
        min_target = entry + risk if bias == "long" else entry - risk
        for t in swing_targets:
            if bias == "long" and t >= min_target:
                targets.append(t)
            elif bias == "short" and t <= min_target:
                targets.append(t)
            if len(targets) >= 2:
                break

    # Fallback: ZC1 at 1.5R, ZC2 at 2.5R
    if len(targets) == 0:
        if bias == "long":
            targets = [entry + 1.5 * risk, entry + 2.5 * risk]
        else:
            targets = [entry - 1.5 * risk, entry - 2.5 * risk]
    elif len(targets) == 1:
        # Have ZC1 structural, add ZC2 as extension
        if bias == "long":
            targets.append(max(targets[0] + risk, entry + 2.5 * risk))
        else:
            targets.append(min(targets[0] - risk, entry - 2.5 * risk))

    return entry, sl, targets


# --------------------------------------------------------------------------- #
# Refinement gates (iter 4 + iter 5)
# --------------------------------------------------------------------------- #


def detect_climax_cooldown(
    daily: pd.DataFrame,
    atr_window: int = 20,
    climax_mult: float = 3.0,
    cooldown_bars: int = 10,
) -> tuple[bool, int | None]:
    """Iter 4: detect a recent single-candle climax.

    Walks the last ``cooldown_bars`` daily candles. If any has a true range
    exceeding ``climax_mult × ATR-{atr_window}`` measured *at that bar*,
    the cooldown is active until ``cooldown_bars`` quiet bars have elapsed.

    Returns ``(in_cooldown, bars_since_climax)``. ``bars_since_climax`` is
    ``None`` when no climax was found in the lookback window.
    """
    if len(daily) < atr_window + 2:
        return False, None
    high = daily["high"].astype(float)
    low = daily["low"].astype(float)
    close = daily["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    rolling_atr = tr.rolling(atr_window).mean()

    last_idx = len(daily) - 1
    earliest = max(0, last_idx - cooldown_bars + 1)
    most_recent_climax: int | None = None
    for i in range(last_idx, earliest - 1, -1):
        atr_i = rolling_atr.iloc[i]
        if pd.isna(atr_i) or atr_i <= 0:
            continue
        if tr.iloc[i] > climax_mult * atr_i:
            most_recent_climax = i
            break
    if most_recent_climax is None:
        return False, None
    bars_since = last_idx - most_recent_climax
    return bars_since < cooldown_bars, bars_since


def find_impulse_leg(
    daily: pd.DataFrame, lookback: int = 60, swing_window: int = 5
) -> tuple[float, float, int, int] | None:
    """Locate the latest daily impulse leg.

    A swing point is a local high/low that strictly dominates ``swing_window``
    bars on each side. The function returns ``(low, high, low_idx, high_idx)``
    for the most recent confirmed swing low and swing high in the last
    ``lookback`` bars, or ``None`` if no clean leg can be identified.
    """
    if len(daily) < lookback or len(daily) < 2 * swing_window + 1:
        return None
    sub = daily.tail(lookback).reset_index(drop=True)
    highs = sub["high"].astype(float).values
    lows = sub["low"].astype(float).values

    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []
    for i in range(swing_window, len(sub) - swing_window):
        win_h = highs[i - swing_window : i + swing_window + 1]
        win_l = lows[i - swing_window : i + swing_window + 1]
        if highs[i] == win_h.max() and (win_h == highs[i]).sum() == 1:
            swing_highs.append((i, float(highs[i])))
        if lows[i] == win_l.min() and (win_l == lows[i]).sum() == 1:
            swing_lows.append((i, float(lows[i])))

    if not swing_highs or not swing_lows:
        return None

    last_low_idx, last_low = swing_lows[-1]
    last_high_idx, last_high = swing_highs[-1]
    return last_low, last_high, last_low_idx, last_high_idx


def chase_gate(
    daily: pd.DataFrame,
    bias: str,
    min_retrace: float = 0.50,
    max_retrace: float = 0.95,
) -> tuple[bool, float | None, str]:
    """Iter 5: reject entries that are still extended near the impulse extreme.

    For a long bias, the leg is swing_low→swing_high and price must have
    pulled back to at least ``min_retrace`` of the leg from the high. For
    a short bias, the leg is swing_high→swing_low and price must have
    rallied at least ``min_retrace`` from the low. Above ``max_retrace``
    the leg is considered invalidated.

    Returns ``(passes, retracement_fraction, reason)``.
    """
    leg = find_impulse_leg(daily)
    if leg is None:
        # Not enough structure — be permissive rather than block.
        return True, None, "no impulse leg detected (permissive pass)"
    low, high, low_idx, high_idx = leg
    if high <= low:
        return True, None, "degenerate leg (permissive pass)"
    last_close = float(daily["close"].iloc[-1])
    leg_size = high - low

    if bias == "long":
        if high_idx < low_idx:
            # The most recent swing high pre-dates the swing low → no
            # confirmed up-leg yet.
            return True, None, "no confirmed up-leg (permissive pass)"
        retrace = (high - last_close) / leg_size
    else:
        if low_idx < high_idx:
            return True, None, "no confirmed down-leg (permissive pass)"
        retrace = (last_close - low) / leg_size

    if retrace < min_retrace:
        return False, retrace, (
            f"chase gate: retrace {retrace:.0%} < min {min_retrace:.0%}"
        )
    if retrace > max_retrace:
        return False, retrace, (
            f"chase gate: retrace {retrace:.0%} > max {max_retrace:.0%} (leg invalidated)"
        )
    return True, retrace, f"retrace {retrace:.0%} inside entry band"


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


@dataclass
class TheoryV2Decision:
    """Diagnostic record of one engine evaluation.

    ``signal`` is populated only when all four gates pass. ``stage`` and
    ``reason`` describe where the evaluation stopped, which is what live
    monitoring will care about.
    """

    coin: str
    bias: str  # "long" | "short" | "neutral"
    daily_confirmed: bool
    setup_valid: bool
    stage: str  # "weekly" | "weekly_cot" | "daily" | "climax_cooldown" | "chase_gate" | "4H" | "1H" | "fired"
    reason: str
    signal: Signal | None = None


class TheoryV2Engine:
    """Stateless evaluator: dataframes in, ``TheoryV2Decision`` out.

    The engine assumes the caller has already trimmed each frame to the
    bars that are visible at the moment of evaluation (``timestamp <= now``).
    It does no look-ahead.
    """

    def __init__(
        self,
        source: str = "theory_v2",
        confidence: float = 0.65,
        cot_history_fn: CotHistoryFn | None = None,
    ):
        self.source = source
        self.confidence = confidence
        self.cot_history_fn = cot_history_fn

    def evaluate(
        self,
        coin: str,
        weekly: pd.DataFrame,
        daily: pd.DataFrame,
        h4: pd.DataFrame,
        h1: pd.DataFrame,
        as_of: pd.Timestamp | None = None,
    ) -> TheoryV2Decision:
        bias, velocity = momentum_bias(weekly)
        bias_source = "momentum"
        breakout_diag: dict | None = None
        if bias == "neutral":
            # iter 18 fallback — range-breakout for iter 16 blind spot.
            rb_bias, breakout_diag = range_breakout_bias(weekly)
            if rb_bias != "neutral":
                bias = rb_bias
                bias_source = "range_breakout"
            else:
                vel_str = f"{velocity:+.2f}" if velocity is not None else "n/a"
                return TheoryV2Decision(
                    coin, bias, False, False, "weekly",
                    f"no weekly bias (momentum velocity={vel_str} ATRs, no range breakout)",
                )

        if self.cot_history_fn is not None:
            ts = as_of if as_of is not None else pd.Timestamp.now(tz="UTC")
            cot_history = self.cot_history_fn(coin, ts)
            passes, bias, reason = weekly_cot_filter(bias, cot_history, ts)
            if not passes:
                return TheoryV2Decision(coin, bias, False, False, "weekly_cot", reason)

        if not daily_confirms(daily, bias):
            return TheoryV2Decision(
                coin, bias, False, False, "daily", "daily does not confirm weekly bias"
            )

        # iter 4 — post-climax cooldown dominates everything else.
        in_cooldown, bars_since = detect_climax_cooldown(daily)
        if in_cooldown:
            return TheoryV2Decision(
                coin, bias, True, False, "climax_cooldown",
                f"climax detected {bars_since} bars ago — cooldown active",
            )

        # iter 5 — chase prevention via impulse-leg retracement.
        chase_ok, retrace, chase_reason = chase_gate(daily, bias)
        if not chase_ok:
            return TheoryV2Decision(coin, bias, True, False, "chase_gate", chase_reason)

        if not four_h_setup_valid(h4, bias):
            return TheoryV2Decision(
                coin, bias, True, False, "4H", "4H setup invalid after daily confirm"
            )

        result = one_h_entry(h1, bias, daily=daily)
        if result is None:
            return TheoryV2Decision(
                coin, bias, True, True, "1H", "1H entry geometry invalid"
            )

        entry_px, sl, targets = result
        direction = Direction.LONG if bias == "long" else Direction.SHORT
        atr = daily_atr(daily)
        risk = abs(entry_px - sl)
        zc1_rr = abs(targets[0] - entry_px) / risk if risk > 0 else 0
        signal = Signal(
            coin=coin,
            direction=direction,
            confidence=self.confidence,
            source=self.source,
            bias_timeframe=Timeframe.WEEKLY,
            setup_timeframe=Timeframe.H4,
            trigger_timeframe=Timeframe.H1,
            invalidation=sl,
            targets=targets,
            metadata={
                "entry_price": entry_px,
                "bias": bias,
                "stop_distance": abs(entry_px - sl) / entry_px,
                "zc1_rr": round(zc1_rr, 2),
                "daily_atr_14": atr,
                "retrace_fraction": retrace,
                "weekly_velocity_atr": round(velocity, 2) if velocity is not None else None,
                "bias_source": bias_source,
                "range_breakout": breakout_diag,
            },
        )
        return TheoryV2Decision(coin, bias, True, True, "fired", chase_reason, signal)

    def evaluate_squeeze_daily(
        self,
        coin: str,
        daily: pd.DataFrame,
        h4: pd.DataFrame,
        h1: pd.DataFrame,
        squeeze_state: "SqueezeDailyState",
        squeeze_cfg: "SqueezeConfig | None" = None,
    ) -> TheoryV2Decision:
        """N6: Daily-cadence squeeze evaluation — bypasses weekly bias gates.

        This is the alternative evaluation path for squeeze breakouts.  Instead
        of waiting for the weekly evaluator to detect a squeeze (which by then
        is already extended), this method checks for a same-day breakout from
        an active squeeze and arms the bias immediately.

        Pipeline:
            squeeze detection  → same-day breakout from active squeeze
            daily confirmation → trend over 10 daily closes must match bias
            climax cooldown    → no daily TR > 3× ATR-20 within last 10 days
            ~~chase gate~~     → SKIPPED (the breakout IS the move)
            4H setup           → trend over 12 4H closes must match bias
            1H entry           → standard swing/ATR floor geometry

        Returns a ``TheoryV2Decision`` with stage ``"squeeze_daily"`` when no
        squeeze breakout is detected, or proceeds through the downstream gates
        as usual.

        NOTE: this path is **not** wired into the default ``evaluate()``.  It
        is only invoked explicitly (e.g. by the N6 A/B harness) so production
        defaults are unchanged until human review.
        """
        # Lazy import to avoid circular dependency
        from trading.crypto.analysis.squeeze_daily import detect_squeeze_daily  # noqa: PLC0415

        sq_bias, sq_diag = detect_squeeze_daily(daily, squeeze_state, squeeze_cfg)

        if sq_bias == "neutral":
            reason = "no squeeze breakout"
            if sq_diag and sq_diag.get("squeeze_active"):
                reason = sq_diag.get("reason", reason)
            return TheoryV2Decision(
                coin, "neutral", False, False, "squeeze_daily", reason,
            )

        bias = sq_bias

        # Daily confirmation — same gate as weekly path
        if not daily_confirms(daily, bias):
            return TheoryV2Decision(
                coin, bias, False, False, "daily",
                "daily does not confirm squeeze bias",
            )

        # Climax cooldown: SKIPPED for squeeze breakouts.
        # Rationale: the squeeze breakout bar is itself a high-volatility
        # expansion — it IS the climax.  Blocking it would prevent the
        # very entry the squeeze path is designed to capture.
        # Same logic as skipping chase_gate.
        in_cooldown = False
        bars_since = None
        cooldown_reason = "climax cooldown skipped (squeeze daily path)"

        # Chase gate: SKIPPED for squeeze breakouts.
        # Rationale: in a squeeze breakout, the move IS the entry signal.
        # The chase gate would reject it because price is near the impulse
        # extreme — but that's exactly where a squeeze breakout should be.
        retrace = None
        chase_reason = "chase gate skipped (squeeze daily path)"

        # 4H setup — same gate as weekly path
        if not four_h_setup_valid(h4, bias):
            return TheoryV2Decision(
                coin, bias, True, False, "4H",
                "4H setup invalid after squeeze daily confirm",
            )

        # 1H entry — same geometry as weekly path
        result = one_h_entry(h1, bias, daily=daily)
        if result is None:
            return TheoryV2Decision(
                coin, bias, True, True, "1H",
                "1H entry geometry invalid (squeeze daily)",
            )

        entry_px, sl, targets = result
        direction = Direction.LONG if bias == "long" else Direction.SHORT
        atr = daily_atr(daily)
        risk = abs(entry_px - sl)
        zc1_rr = abs(targets[0] - entry_px) / risk if risk > 0 else 0
        signal = Signal(
            coin=coin,
            direction=direction,
            confidence=self.confidence,
            source=self.source,
            bias_timeframe=Timeframe.WEEKLY,  # squeeze is a daily event but conceptually replaces weekly bias
            setup_timeframe=Timeframe.H4,
            trigger_timeframe=Timeframe.H1,
            invalidation=sl,
            targets=targets,
            metadata={
                "entry_price": entry_px,
                "bias": bias,
                "stop_distance": abs(entry_px - sl) / entry_px,
                "zc1_rr": round(zc1_rr, 2),
                "daily_atr_14": atr,
                "retrace_fraction": retrace,
                "weekly_velocity_atr": None,  # not applicable for squeeze
                "bias_source": "squeeze_daily",
                "squeeze_diagnostic": sq_diag,
            },
        )
        return TheoryV2Decision(
            coin, bias, True, True, "fired", chase_reason, signal,
        )


# --------------------------------------------------------------------------- #
# Strategy wiring (live data via scripts/data_loader)
# --------------------------------------------------------------------------- #


#: Per-timeframe lookback windows (days). Each is sized so the engine has
#: more than enough bars for its rolling-window calculations even when the
#: local data has coverage gaps.
DEFAULT_LOOKBACK_DAYS: dict[str, int] = {
    "1W": 365 * 5,  # weekly bias needs 8 bars + headroom for gap regions
    "1D": 365,      # daily SMA-20 + ATR-14 + chase-leg lookback
    "4H": 90,       # 4H SMA-12 + buffer
    "1H": 30,       # 1H 24-bar swing window
}


def _load_frames(coin: str) -> dict[str, pd.DataFrame]:
    """Fetch the four timeframes via the project's data_loader.

    Imported lazily so unit tests for the pure engine never need a data
    backend.
    """
    import sys
    from pathlib import Path

    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root / "scripts"))
    import data_loader  # type: ignore

    end = pd.Timestamp.now(tz="UTC").normalize()
    out: dict[str, pd.DataFrame] = {}
    for label, tf in (("weekly", "1W"), ("daily", "1D"), ("4H", "4H"), ("1H", "1H")):
        start = end - pd.Timedelta(days=DEFAULT_LOOKBACK_DAYS[tf])
        out[label] = data_loader.load(coin, tf, start, end)
    return out


def _default_cot_history_fn() -> CotHistoryFn:
    """Build a live-mode COT provider backed by the persisted history cache.

    Per ``cot_integration.yaml`` ETH COT is weaker than BTC COT; both coins
    use BTC COT as the market-wide positioning reference.
    """
    from trading.crypto.cot.cot_gate import load_cached_cot_history

    btc_history = load_cached_cot_history("BTC")

    def _provider(_coin: str, _as_of: pd.Timestamp) -> pd.DataFrame:
        return btc_history

    return _provider


def evaluate_coin_live(coin: str, engine: TheoryV2Engine | None = None) -> TheoryV2Decision:
    """Convenience entry-point: load data and evaluate one coin."""
    engine = engine or TheoryV2Engine(cot_history_fn=_default_cot_history_fn())
    frames = _load_frames(coin)
    return engine.evaluate(
        coin,
        frames["weekly"],
        frames["daily"],
        frames["4H"],
        frames["1H"],
    )


def build_signals_for_coins(coins: list[str]) -> tuple[list[Signal], list[TheoryV2Decision]]:
    """Evaluate each coin and return (fired signals, all decisions)."""
    engine = TheoryV2Engine(cot_history_fn=_default_cot_history_fn())
    decisions: list[TheoryV2Decision] = []
    signals: list[Signal] = []
    for coin in coins:
        try:
            decision = evaluate_coin_live(coin, engine)
        except Exception as exc:  # noqa: BLE001
            logger.warning("theory_v2 evaluation failed for %s: %s", coin, exc)
            continue
        decisions.append(decision)
        if decision.signal is not None:
            signals.append(decision.signal)
    return signals, decisions


# --------------------------------------------------------------------------- #
# Live analyzer CLI — `python -m trading.crypto.theory_v2 --coins BTC ETH`
# --------------------------------------------------------------------------- #


def _format_decision(decision: TheoryV2Decision) -> str:
    """Render one coin's decision as a human-readable action card."""
    bar = "─" * 60
    header = f"{bar}\n{decision.coin}  bias={decision.bias.upper()}"
    if decision.signal is not None:
        sig = decision.signal
        m = sig.metadata
        zc1_rr = m.get("zc1_rr")
        atr = m.get("daily_atr_14")
        retrace = m.get("retrace_fraction")
        velocity = m.get("weekly_velocity_atr")
        lines = [
            f"  ACTION    : {sig.direction.value.upper()}",
            f"  Entry     : {m.get('entry_price'):.2f}",
            f"  Stop-loss : {sig.invalidation:.2f}",
        ]
        if len(sig.targets) >= 2:
            lines.append(f"  ZC1 (80%) : {sig.targets[0]:.2f}  (RR {zc1_rr})")
            lines.append(f"  ZC2 (20%) : {sig.targets[1]:.2f}")
        elif sig.targets:
            lines.append(f"  Target    : {sig.targets[0]:.2f}  (RR {zc1_rr})")
        lines.append(f"  Risk %    : {m.get('stop_distance', 0)*100:.2f}%")
        if atr is not None:
            lines.append(f"  Daily ATR : {atr:.2f}")
        if retrace is not None:
            lines.append(f"  Retrace   : {retrace*100:.0f}% of impulse leg")
        if velocity is not None:
            lines.append(f"  Momentum  : {velocity:+.2f} weekly ATRs / 4w")
        body = "\n".join(lines)
        return f"{header}\n  STAGE     : FIRED — {decision.reason}\n{body}"
    return (
        f"{header}\n"
        f"  ACTION    : STAND ASIDE\n"
        f"  STAGE     : {decision.stage}\n"
        f"  REASON    : {decision.reason}"
    )


def analyze_live(coins: list[str]) -> int:
    """Run the engine against live data for ``coins`` and print action cards.

    Returns a process exit code: 0 if at least one coin was evaluated, 2 if
    every evaluation failed (e.g. data backend missing).
    """
    print(f"theory_v2 live analysis — coins={coins}")
    print(f"as of:  {pd.Timestamp.now(tz='UTC').isoformat()}\n")

    engine = TheoryV2Engine(cot_history_fn=_default_cot_history_fn())
    n_evaluated = 0
    fired: list[Signal] = []
    for coin in coins:
        try:
            decision = evaluate_coin_live(coin, engine)
        except Exception as exc:  # noqa: BLE001
            print(f"{coin}: evaluation failed — {exc}")
            continue
        n_evaluated += 1
        print(_format_decision(decision))
        if decision.signal is not None:
            fired.append(decision.signal)

    print("\n" + "=" * 60)
    print(f"Summary: {len(fired)}/{n_evaluated} coin(s) firing fresh entries")
    if fired:
        for s in fired:
            print(f"  - {s.coin} {s.direction.value} @ {s.metadata.get('entry_price'):.2f}")
    return 0 if n_evaluated > 0 else 2


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="theory_v2 live analyzer")
    parser.add_argument(
        "--coins",
        nargs="+",
        default=["BTC", "ETH"],
        help="coin tickers to evaluate (default: BTC ETH)",
    )
    args = parser.parse_args()
    return analyze_live([c.upper() for c in args.coins])


if __name__ == "__main__":
    raise SystemExit(_main())
