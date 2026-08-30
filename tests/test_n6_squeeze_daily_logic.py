"""Behavioral test of the isolated N6 daily-squeeze breakout detection.

Builds a synthetic daily series where a volatility compression (tight BB
width) is sustained for >7 days and then breaks out, and asserts that
``detect_squeeze_daily`` arms the correct bias on the breakout bar itself —
this is the whole point of the N6 daily-cadence path vs. the (REJECT) N5
weekly path.
"""

import numpy as np
import pandas as pd
import pytest

from trading.crypto.analysis.squeeze_daily import (
    SqueezeConfig,
    SqueezeDailyState,
    detect_squeeze_daily,
)


def _make_daily(n: int, start: float = 100.0) -> pd.DataFrame:
    """Random-walk daily OHLC with modest volatility."""
    rng = np.random.default_rng(42)
    closes = [start]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.normal(0, 0.02)))
    closes = np.array(closes)
    high = closes * 1.01
    low = closes * 0.99
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    return pd.DataFrame({"open": closes, "high": high, "low": low, "close": closes}, index=idx)


def test_squeeze_daily_breakout_long_armed_on_breakout_bar():
    """A sustained silent squeeze followed by an up-breakout arms 'long'."""
    # 300 quiet bars builds the 120d percentile history with a wide prior
    # distribution, then 12 bars of extreme compression (tight BB).
    daily = _make_daily(300)
    # Force the last 12 closes into a tight range so BB width is tiny.
    base = daily["close"].iloc[-13]
    for i in range(1, 13):
        daily.loc[daily.index[-13 + i], "close"] = base * (1 + 0.001 * i)
        daily.loc[daily.index[-13 + i], "high"] = daily["close"].iloc[-13 + i] * 1.005
        daily.loc[daily.index[-13 + i], "low"] = daily["close"].iloc[-13 + i] * 0.995

    state = SqueezeDailyState()

    # Walk each bar so the streak counter accumulates.
    for i in range(100, len(daily)):
        detect_squeeze_daily(daily.iloc[: i + 1], state, SqueezeConfig())

    # Now add a breaking-up bar at the end.
    prev_close = daily["close"].iloc[-1]
    breakout_row = pd.DataFrame(
        {
            "open": [prev_close],
            "high": [prev_close * 1.08],
            "low": [prev_close * 0.995],
            "close": [prev_close * 1.07],
        },
        index=[daily.index[-1] + pd.Timedelta(days=1)],
    )
    daily2 = pd.concat([daily, breakout_row])
    state2 = SqueezeDailyState()
    # re-walk through the squeeze into the breakout
    bias = None
    for i in range(100, len(daily2)):
        b, diag = detect_squeeze_daily(daily2.iloc[: i + 1], state2, SqueezeConfig())
        if b != "neutral":
            bias = b

    assert bias == "long", f"expected long breakout, got {bias}"


def test_squeeze_daily_returns_neutral_without_squeeze():
    """A volatile non-compressed series never fires."""
    daily = _make_daily(300)
    state = SqueezeDailyState()
    bias, diag = detect_squeeze_daily(daily, state, SqueezeConfig())
    # Volatile random walk → no sustained compression → no breakout.
    assert bias in ("neutral",)
    assert state.active is False


def test_squeeze_daily_insufficient_history_neutral():
    """Too little history returns neutral and does not crash."""
    daily = _make_daily(30)
    state = SqueezeDailyState()
    bias, diag = detect_squeeze_daily(daily, state, SqueezeConfig())
    assert bias == "neutral"


def test_breakout_fires_when_breakout_bar_remains_below_squeeze_threshold():
    """A modest breakout must not be swallowed by the updated range."""
    n = 160
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    close = np.full(n, 100.0)
    high = np.full(n, 100.2)
    low = np.full(n, 99.8)
    daily = pd.DataFrame({"open": close, "high": high, "low": low, "close": close}, index=idx)
    state = SqueezeDailyState()

    # Establish a qualifying active squeeze one bar at a time.
    for i in range(140, n):
        bias, _ = detect_squeeze_daily(daily.iloc[: i + 1], state, SqueezeConfig())
        assert bias == "neutral"
    assert state.active is True
    old_high = state.squeeze_high

    # The close breaks the prior range, but BB width remains under 3.5%.
    breakout = daily.copy()
    breakout.loc[idx[-1], "close"] = 101.5
    breakout.loc[idx[-1], "open"] = 100.0
    breakout.loc[idx[-1], "high"] = 101.6
    breakout.loc[idx[-1], "low"] = 101.4
    bias, diag = detect_squeeze_daily(breakout, state, SqueezeConfig())

    assert old_high < 101.5
    assert bias == "long"
    assert diag is not None
    assert diag["squeeze_high"] == round(old_high, 2)
