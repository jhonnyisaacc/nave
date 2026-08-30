"""N6 daily-cadence squeeze evaluator — same-day breakout detection.

This module is the daily-cadence counterpart to the (N5 REJECT) weekly
``squeeze_detector``.  N5 wired a squeeze detector as a 4th weekly bias
source and failed (0% WR, -7.0R, 100% FP) because by the time the weekly
evaluator ran, the squeeze had already exploded and the price was already
extended.  This module instead detects breakouts **on the bar they happen**
so the engine can arm a bias immediately instead of waiting for the next
weekly evaluation.

The weekly-gate approach is deliberately omitted from this isolated N6 path:
the same-day breakout bar IS the entry signal, so the downstream gates that
would reject an extended move (chase gate, climax cooldown) are skipped in
the TheoryV2Engine ``evaluate_squeeze_daily`` path.

Squeeze definition:
    BB width (20d) below 25th percentile of its own 120-day history OR below
    3.5% absolute, sustained >= 7 consecutive days.

Breakout definition:
    Close beyond the last compression range ± breakout_atr_mult × ATR-14.

Timeout:
    If the squeeze has been inactive for >14 days with no breakout, the
    detector returns neutral (disarm).  This prevents stale squeeze state
    from firing on unrelated moves.

Config/helpers (SqueezeConfig, _atr, _bb_width) are defined locally so this
module is self-contained and depends only on ``pandas`` — it does not import
from the N5 weekly-bias detector.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class SqueezeConfig:
    """Tunable parameters for the squeeze detector."""

    bb_window: int = 20              # Bollinger Band lookback
    pct_window: int = 120            # rolling percentile window
    pct_threshold: float = 25.0      # BB width must be below this percentile
    abs_threshold: float = 3.5       # OR below this absolute BB width (%)
    min_streak: int = 7              # minimum consecutive squeeze days
    breakout_atr_mult: float = 0.5   # breakout = close beyond range ± mult × ATR
    atr_window: int = 14             # ATR lookback for breakout sizing


def _bb_width(close: pd.Series, window: int) -> pd.Series:
    """Bollinger Band width as a percentage of the middle band."""
    sma = close.rolling(window).mean()
    std = close.rolling(window).std()
    return (2 * std / sma) * 100


def _atr(daily: pd.DataFrame, window: int) -> pd.Series:
    """Average True Range."""
    high = daily["high"].astype(float)
    low = daily["low"].astype(float)
    close = daily["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window).mean()


@dataclass
class SqueezeDailyState:
    """Mutable state tracker for daily squeeze evaluation.

    Maintained across daily bars so the evaluator knows whether a squeeze
    is currently active, how long the streak is, and what the compression
    range is.  Updated by ``update()`` after each daily bar.
    """

    active: bool = False
    streak_days: int = 0
    squeeze_high: float = 0.0
    squeeze_low: float = 0.0
    bb_width_mean: float = 0.0
    bars_since_end: int = 0  # days since squeeze went inactive without breakout
    _ended_without_breakout: bool = False
    breakout_fired: bool = False  # True after a breakout was returned for this cycle


def update_squeeze_state(
    daily: pd.DataFrame,
    state: SqueezeDailyState,
    cfg: SqueezeConfig | None = None,
) -> SqueezeDailyState:
    """Update squeeze state with the latest daily bar.

    Call this once per day with the full daily history (or at least the last
    ~140 bars).  The state object is mutated in place and also returned for
    convenience.

    This is a pure state-tracker — it does NOT detect breakouts.  Use
    ``detect_squeeze_daily()`` for breakout detection.
    """
    cfg = cfg or SqueezeConfig()
    min_bars = max(cfg.pct_window + cfg.bb_window, cfg.atr_window + cfg.bb_window) + 2
    if daily.empty or len(daily) < min_bars:
        return state

    close = daily["close"].astype(float)
    high = daily["high"].astype(float)
    low = daily["low"].astype(float)

    bb = _bb_width(close, cfg.bb_window)
    bb_pctl = bb.rolling(cfg.pct_window, min_periods=60).rank(pct=True) * 100

    squeeze_rel = bb_pctl < cfg.pct_threshold
    squeeze_abs = bb < cfg.abs_threshold
    is_squeeze = squeeze_rel | squeeze_abs

    last = len(daily) - 1
    last_squeeze = bool(is_squeeze.iloc[last])

    if last_squeeze:
        # Still in squeeze — update streak
        groups = (is_squeeze != is_squeeze.shift()).cumsum()
        streak = is_squeeze.groupby(groups).cumsum()
        state.active = True
        state.streak_days = int(streak.iloc[last])
        # Update compression range
        streak_start = max(0, last - state.streak_days + 1)
        state.squeeze_high = float(high.iloc[streak_start:last + 1].max())
        state.squeeze_low = float(low.iloc[streak_start:last + 1].min())
        state.bb_width_mean = float(bb.iloc[streak_start:last + 1].mean())
        state._ended_without_breakout = False
        state.bars_since_end = 0
        # Reset breakout_fired when a new squeeze cycle begins
        if state.streak_days == 1:
            state.breakout_fired = False
    else:
        # Not in squeeze
        if state.active:
            # Squeeze just ended — start counting bars since end
            state.active = False
            state._ended_without_breakout = True
            state.bars_since_end = 1
        elif state._ended_without_breakout:
            state.bars_since_end += 1
            if state.bars_since_end > 14:
                # Timeout — disarm
                state._ended_without_breakout = False
                state.bars_since_end = 0
                state.streak_days = 0

    return state


def detect_squeeze_daily(
    daily: pd.DataFrame,
    state: SqueezeDailyState,
    cfg: SqueezeConfig | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Detect whether the latest bar is a squeeze breakout (same-day).

    Unlike the (removed) N5 ``detect_squeeze()`` which looks backward for ended
    squeezes, this function checks whether the *current* bar is a breakout from
    an active squeeze.  This enables daily-cadence evaluation: the engine can
    arm a bias on the same day the breakout happens.

    Args:
        daily: Full daily OHLCV history (or at least ~140 bars).
        state: Mutable squeeze state tracker (updated in place).
        cfg: Squeeze parameters (defaults match N5 discovery).

    Returns:
        ``(bias, diagnostic)`` where bias is ``"long"``, ``"short"``, or
        ``"neutral"``.  Diagnostic is ``None`` when there's no squeeze
        context at all, or a dict with squeeze metadata.
    """
    cfg = cfg or SqueezeConfig()
    min_bars = max(cfg.pct_window + cfg.bb_window, cfg.atr_window + cfg.bb_window) + 2
    if daily.empty or len(daily) < min_bars:
        return "neutral", None

    close = daily["close"].astype(float)
    high = daily["high"].astype(float)
    low = daily["low"].astype(float)
    atr = _atr(daily, cfg.atr_window)

    last = len(daily) - 1
    last_close = float(close.iloc[last])
    last_atr = float(atr.iloc[last]) if not pd.isna(atr.iloc[last]) else 0

    # Check the current bar against the *prior* compression range before
    # updating state.  A breakout can leave BB width below the squeeze
    # threshold, so updating first would include the breakout bar in the
    # range and incorrectly keep the squeeze active.
    if (
        state.active
        and state.streak_days >= cfg.min_streak
        and not state.breakout_fired
    ):
        buffer = cfg.breakout_atr_mult * last_atr
        if last_close > state.squeeze_high + buffer:
            state.breakout_fired = True
            return "long", {
                "squeeze_streak": state.streak_days,
                "squeeze_high": round(state.squeeze_high, 2),
                "squeeze_low": round(state.squeeze_low, 2),
                "bb_width_mean": round(state.bb_width_mean, 2),
                "breakout_close": round(last_close, 2),
                "atr_14": round(last_atr, 2),
                "bars_since_squeeze_end": 0,
                "direction": "long",
                "reason": f"squeeze {state.streak_days}d → long breakout (during squeeze)",
            }
        if last_close < state.squeeze_low - buffer:
            state.breakout_fired = True
            return "short", {
                "squeeze_streak": state.streak_days,
                "squeeze_high": round(state.squeeze_high, 2),
                "squeeze_low": round(state.squeeze_low, 2),
                "bb_width_mean": round(state.bb_width_mean, 2),
                "breakout_close": round(last_close, 2),
                "atr_14": round(last_atr, 2),
                "bars_since_squeeze_end": 0,
                "direction": "short",
                "reason": f"squeeze {state.streak_days}d → short breakout (during squeeze)",
            }

    # Update state with latest bar after checking the prior range.
    update_squeeze_state(daily, state, cfg)

    # Case 1: squeeze is currently active — no breakout yet
    if state.active:
        return "neutral", {
            "squeeze_active": True,
            "streak_days": state.streak_days,
            "bb_width_mean": round(state.bb_width_mean, 2),
            "squeeze_high": round(state.squeeze_high, 2),
            "squeeze_low": round(state.squeeze_low, 2),
            "reason": f"squeeze active ({state.streak_days}d), no breakout yet",
        }

    # Case 2: squeeze recently ended — check for breakout
    if state._ended_without_breakout and state.streak_days >= cfg.min_streak and not state.breakout_fired:
        buffer = cfg.breakout_atr_mult * last_atr

        if last_close > state.squeeze_high + buffer:
            state.breakout_fired = True
            return "long", {
                "squeeze_streak": state.streak_days,
                "squeeze_high": round(state.squeeze_high, 2),
                "squeeze_low": round(state.squeeze_low, 2),
                "bb_width_mean": round(state.bb_width_mean, 2),
                "breakout_close": round(last_close, 2),
                "atr_14": round(last_atr, 2),
                "bars_since_squeeze_end": state.bars_since_end,
                "direction": "long",
                "reason": (
                    f"squeeze {state.streak_days}d → long breakout "
                    f"({state.bars_since_end}d after squeeze end)"
                ),
            }
        if last_close < state.squeeze_low - buffer:
            state.breakout_fired = True
            return "short", {
                "squeeze_streak": state.streak_days,
                "squeeze_high": round(state.squeeze_high, 2),
                "squeeze_low": round(state.squeeze_low, 2),
                "bb_width_mean": round(state.bb_width_mean, 2),
                "breakout_close": round(last_close, 2),
                "atr_14": round(last_atr, 2),
                "bars_since_squeeze_end": state.bars_since_end,
                "direction": "short",
                "reason": (
                    f"squeeze {state.streak_days}d → short breakout "
                    f"({state.bars_since_end}d after squeeze end)"
                ),
            }

    # Case 3: no squeeze context
    return "neutral", None