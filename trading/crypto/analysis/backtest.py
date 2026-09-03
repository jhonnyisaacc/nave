"""Historical validation — delegates to momentum workflow (single backtest path)."""

from __future__ import annotations

from typing import Any

MOMENTUM_PERIOD_ORDER = [
    "2017-bull+2018-bear",
    "2019-recovery",
    "2020-covid-crash",
    "2020-recovery+2021-ATH",
    "2022-bear",
    "2023-recovery",
    "2024-ETF-approval",
    "2024-2025-bull",
    "TODAY",
]
MOMENTUM_PERIODS = {
    "2017-bull+2018-bear": ("2017-01-01", "2018-12-31"),
    "2019-recovery": ("2019-01-01", "2019-12-31"),
    "2020-covid-crash": ("2020-01-01", "2020-06-30"),
    "2020-recovery+2021-ATH": ("2020-07-01", "2021-12-31"),
    "2022-bear": ("2022-01-01", "2022-12-31"),
    "2023-recovery": ("2023-01-01", "2023-12-31"),
    "2024-ETF-approval": ("2024-01-01", "2024-06-30"),
    "2024-2025-bull": ("2024-07-01", "2025-03-31"),
}
HISTORICAL_PERIODS = [period for period in MOMENTUM_PERIOD_ORDER if period != "TODAY"]


def _workflow():
    """Load the legacy file-backed workflow only for an explicit full backtest."""
    from trading.crypto.momentum.workflow import run_period_backtest, write_period_artifact

    return run_period_backtest, write_period_artifact


def run_all_periods(
    *,
    symbols: list[str] | None = None,
    include_today: bool = False,
    write_artifacts: bool = True,
    skip_baseline_compare: bool = False,
) -> dict[str, Any]:
    """Run momentum+COT historical backtest for every named regime."""
    run_period_backtest, write_period_artifact = _workflow()
    periods = list(HISTORICAL_PERIODS)
    if include_today:
        periods.append("TODAY")
    selected = [s.upper() for s in (symbols or ["BTC", "ETH"])]
    payloads: dict[str, Any] = {}
    for period in periods:
        print(f"[backtest] {period} …", flush=True)
        payload = run_period_backtest(
            period,
            symbols=selected,
            skip_baseline_compare=skip_baseline_compare,
        )
        if write_artifacts:
            write_period_artifact(payload)
        payloads[period] = payload
    return summarize_backtests(payloads)


def summarize_backtests(payloads: dict[str, Any]) -> dict[str, Any]:
    """Aggregate per-period metrics into a confidence-oriented summary."""
    rows: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    partial_periods: list[str] = []
    losing_periods: list[str] = []

    for period, payload in payloads.items():
        complete = bool((payload.get("coverage") or {}).get("complete", False))
        if not complete:
            partial_periods.append(period)
        pooled = payload.get("pooled") or {}
        metrics = pooled.get("metrics") or {}
        trade_count = int(pooled.get("trade_count", 0))
        expectancy = float(metrics.get("expectancy") or 0.0)
        if trade_count > 0 and expectancy < 0:
            losing_periods.append(period)

        for symbol, result in (payload.get("results") or {}).items():
            sym_metrics = result.get("metrics") or {}
            rows.append(
                {
                    "period": period,
                    "symbol": symbol,
                    "coverage_complete": bool((result.get("coverage") or {}).get("complete", complete)),
                    "trade_count": int(result.get("trade_count", 0)),
                    "win_rate": sym_metrics.get("win_rate"),
                    "expectancy": sym_metrics.get("expectancy"),
                    "max_drawdown": sym_metrics.get("max_drawdown"),
                    "pct_reaching_8": sym_metrics.get("pct_reaching_8"),
                }
            )
            all_trades.extend(result.get("trades") or [])

    total_trades = len(all_trades)
    wins = sum(1 for t in all_trades if float(t.get("r_multiple", 0)) > 0)
    pooled_wr = wins / total_trades if total_trades else 0.0
    pooled_exp = (
        sum(float(t.get("r_multiple", 0)) for t in all_trades) / total_trades
        if total_trades
        else 0.0
    )
    periods_with_trades = sum(
        1 for p in payloads.values() if int((p.get("pooled") or {}).get("trade_count", 0)) > 0
    )
    n_periods = len(payloads)

    if total_trades < 15:
        confidence = "low"
        confidence_reason = "Too few pooled trades for statistical stability."
    elif partial_periods:
        confidence = "medium"
        confidence_reason = (
            f"Partial data coverage in: {', '.join(partial_periods)}. "
            "Pre-2022 regimes lack historical COT replay."
        )
    elif losing_periods and len(losing_periods) > n_periods // 2:
        confidence = "low"
        confidence_reason = f"Negative expectancy in {len(losing_periods)}/{n_periods} regimes."
    elif pooled_exp >= 0.5 and pooled_wr >= 0.55:
        confidence = "high"
        confidence_reason = (
            f"Pooled {total_trades} trades, {pooled_wr:.0%} win rate, {pooled_exp:+.2f}R expectancy "
            f"across {periods_with_trades}/{n_periods} active regimes."
        )
    elif pooled_exp >= 0.25 and pooled_wr >= 0.50:
        confidence = "medium"
        confidence_reason = (
            f"Pooled edge present ({pooled_exp:+.2f}R, {pooled_wr:.0%} WR) but not uniform — "
            f"weak regimes: {', '.join(losing_periods) or 'none'}."
        )
    else:
        confidence = "low"
        confidence_reason = (
            f"Pooled expectancy {pooled_exp:+.2f}R / win rate {pooled_wr:.0%} below deployment bar."
        )

    return {
        "periods": payloads,
        "rows": rows,
        "pooled": {
            "trade_count": total_trades,
            "win_rate": round(pooled_wr, 4),
            "expectancy": round(pooled_exp, 4),
            "periods_with_trades": periods_with_trades,
            "period_count": n_periods,
        },
        "partial_periods": partial_periods,
        "losing_periods": losing_periods,
        "confidence": confidence,
        "confidence_reason": confidence_reason,
        "period_catalog": MOMENTUM_PERIODS,
    }
