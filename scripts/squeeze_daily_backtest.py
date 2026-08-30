#!/usr/bin/env python3
"""
squeeze_daily_backtest — N6 A/B experiment harness.

Compares:
  Control  = weekly-cadence TheoryV2Engine (squeeze OFF, production default)
  Treatment = weekly-cadence + daily squeeze path (squeeze ON)

The treatment adds squeeze trades on top of the existing weekly trades.
Weekly trades are identical in both arms — the squeeze path is additive.

Usage:
    python scripts/squeeze_daily_backtest.py [--coins BTC ETH]

Output is printed and also written to
``docs/analysis/raw/squeeze_daily_validation_{ts}.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import data_loader  # noqa: E402
from data_loader import DataNotFoundError  # noqa: E402

from trading.crypto.analysis.squeeze_daily import SqueezeDailyState  # noqa: E402
from trading.crypto.cot.cot_gate import load_cached_cot_history  # noqa: E402
from trading.crypto.theory_v2 import TheoryV2Engine  # noqa: E402


# --------------------------------------------------------------------------- #
# Periods — same 8 in-sample + OOS 2026
# --------------------------------------------------------------------------- #

PERIODS: dict[str, tuple[str, str]] = {
    "2017-bull+2018-bear": ("2017-01-01", "2018-12-31"),
    "2019-recovery": ("2019-01-01", "2019-12-31"),
    "2020-covid-crash": ("2020-01-01", "2020-06-30"),
    "2020-recovery+2021-ATH": ("2020-07-01", "2021-12-31"),
    "2022-bear": ("2022-01-01", "2022-12-31"),
    "2023-recovery": ("2023-01-01", "2023-12-31"),
    "2024-ETF-approval": ("2024-01-01", "2024-06-30"),
    "2024-2025-bull": ("2024-07-01", "2025-03-31"),
    # Frozen at the last bar in the committed validation evidence.  Keeping
    # this boundary explicit prevents a later Binance refresh from silently
    # changing the acceptance result.
    "2026-OOS": ("2026-01-01", "2026-08-26"),
}


def _input_snapshot(coins: list[str]) -> dict[str, Any]:
    """Hash every local market/COT input used by the validation rerun."""
    paths = [
        PROJECT_ROOT / "data" / "binance_cache" / f"{coin}_{tf}.parquet"
        for coin in coins
        for tf in ("1h", "4h", "1d", "1w")
    ]
    paths.append(Path.home() / ".cache" / "nave" / "cot" / "history_cot.json")
    files: list[dict[str, Any]] = []
    for path in sorted(set(paths), key=str):
        try:
            display_path = str(path.relative_to(PROJECT_ROOT))
        except ValueError:
            display_path = "~/.cache/nave/cot/history_cot.json"
        item: dict[str, Any] = {"path": display_path, "exists": path.is_file()}
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            item.update({"sha256": digest.hexdigest(), "size": path.stat().st_size})
        files.append(item)
    return {"algorithm": "sha256", "files": files}


# --------------------------------------------------------------------------- #
# Outcome resolver (same as theory_v2_backtest.py)
# --------------------------------------------------------------------------- #


def _resolve_zc_outcome(
    h1_forward: pd.DataFrame,
    direction: str,
    entry: float,
    sl: float,
    targets: list[float],
) -> tuple[str | None, float]:
    """ZC1/ZC2 partial-exit resolver (identical to theory_v2_backtest.py)."""
    if h1_forward.empty or not targets:
        return None, 0.0

    risk = abs(entry - sl)
    if risk <= 0:
        return None, 0.0

    zc1 = targets[0]
    zc2 = targets[1] if len(targets) > 1 else zc1

    zc1_hit = False
    trail_sl = sl

    for _, row in h1_forward.iterrows():
        high = float(row["high"])
        low = float(row["low"])

        if direction == "long":
            if not zc1_hit and low <= sl:
                return "incorrect", -1.0
            if zc1_hit and low <= trail_sl:
                zc1_reward = (zc1 - entry) / risk
                trail_reward = (trail_sl - entry) / risk
                total = 0.8 * zc1_reward + 0.2 * trail_reward
                return "correct", total
            if not zc1_hit and high >= zc1:
                zc1_hit = True
                trail_sl = entry
            if zc1_hit and high >= zc2:
                zc1_reward = (zc1 - entry) / risk
                zc2_reward = (zc2 - entry) / risk
                total = 0.8 * zc1_reward + 0.2 * zc2_reward
                return "correct", total
        else:
            if not zc1_hit and high >= sl:
                return "incorrect", -1.0
            if zc1_hit and high >= trail_sl:
                zc1_reward = (entry - zc1) / risk
                trail_reward = (entry - trail_sl) / risk
                total = 0.8 * zc1_reward + 0.2 * trail_reward
                return "correct", total
            if not zc1_hit and low <= zc1:
                zc1_hit = True
                trail_sl = entry
            if zc1_hit and low <= zc2:
                zc1_reward = (entry - zc1) / risk
                zc2_reward = (entry - zc2) / risk
                total = 0.8 * zc1_reward + 0.2 * zc2_reward
                return "correct", total

    if zc1_hit:
        zc1_reward = abs(zc1 - entry) / risk
        last_close = float(h1_forward["close"].iloc[-1])
        if direction == "long":
            trail_reward = (last_close - entry) / risk
        else:
            trail_reward = (entry - last_close) / risk
        total = 0.8 * zc1_reward + 0.2 * trail_reward
        return "correct", total
    return None, 0.0


# --------------------------------------------------------------------------- #
# Weekly walker (control — identical to theory_v2_backtest.py)
# --------------------------------------------------------------------------- #


def _walk_weekly(
    coin: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    engine: TheoryV2Engine,
) -> dict[str, Any]:
    """Walk weekly periods — same as theory_v2_backtest.py._walk_period."""
    try:
        weekly_full = data_loader.load(coin, "1W", start - pd.Timedelta(days=120), end)
        daily_full = data_loader.load(coin, "1D", start - pd.Timedelta(days=120), end)
        h4_full = data_loader.load(coin, "4H", start - pd.Timedelta(days=60), end)
        h1_full = data_loader.load(coin, "1H", start - pd.Timedelta(days=60), end + pd.Timedelta(days=14))
    except DataNotFoundError as exc:
        return {"skipped": True, "reason": str(exc)}

    stats = {
        "fired": 0,
        "correct": 0,
        "incorrect": 0,
        "unresolved": 0,
        "total_r": 0.0,
        "trades": [],
        "stage_counts": {},
    }

    weeks = pd.date_range(start=start, end=end, freq="W-MON", tz="UTC")
    for week_start in weeks:
        weekly_slice = weekly_full[weekly_full["timestamp"] <= week_start]
        daily_slice = daily_full[daily_full["timestamp"] <= week_start]
        h4_slice = h4_full[h4_full["timestamp"] <= week_start]
        h1_slice = h1_full[h1_full["timestamp"] <= week_start]
        decision = engine.evaluate(
            coin, weekly_slice, daily_slice, h4_slice, h1_slice, as_of=week_start
        )
        stats["stage_counts"][decision.stage] = stats["stage_counts"].get(decision.stage, 0) + 1

        if decision.signal is None:
            continue

        sig = decision.signal
        entry = float(sig.metadata["entry_price"])
        sl = float(sig.invalidation)
        targets = [float(t) for t in sig.targets]
        direction = sig.direction.value

        forward = h1_full[
            (h1_full["timestamp"] > week_start)
            & (h1_full["timestamp"] <= week_start + pd.Timedelta(days=14))
        ]
        outcome, pnl_r = _resolve_zc_outcome(forward, direction, entry, sl, targets)
        stats["fired"] += 1
        stats["total_r"] += pnl_r
        if outcome == "correct":
            stats["correct"] += 1
        elif outcome == "incorrect":
            stats["incorrect"] += 1
        else:
            stats["unresolved"] += 1
        stats["trades"].append({
            "date": str(week_start.date()),
            "direction": direction,
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "targets": [round(t, 2) for t in targets],
            "outcome": outcome,
            "pnl_r": round(pnl_r, 2),
            "bias_source": sig.metadata.get("bias_source", "unknown"),
        })

    return stats


# --------------------------------------------------------------------------- #
# Daily squeeze walker (treatment additions)
# --------------------------------------------------------------------------- #


def _walk_daily_squeeze(
    coin: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    engine: TheoryV2Engine,
) -> dict[str, Any]:
    """Walk daily bars looking for squeeze breakouts.

    This captures trades that the weekly path misses because the squeeze
    breakout happens and resolves within a single week.
    """
    try:
        daily_full = data_loader.load(coin, "1D", start - pd.Timedelta(days=180), end)
        h4_full = data_loader.load(coin, "4H", start - pd.Timedelta(days=90), end)
        h1_full = data_loader.load(coin, "1H", start - pd.Timedelta(days=60), end + pd.Timedelta(days=14))
    except DataNotFoundError as exc:
        return {"skipped": True, "reason": str(exc)}

    stats = {
        "fired": 0,
        "correct": 0,
        "incorrect": 0,
        "unresolved": 0,
        "total_r": 0.0,
        "trades": [],
        "stage_counts": {},
        "squeeze_events": 0,
        "squeeze_active_days": 0,
    }

    state = SqueezeDailyState()
    cooldown_until: pd.Timestamp | None = None

    days = pd.date_range(start=start, end=end, freq="D", tz="UTC")
    for day in days:
        # Skip if in cooldown from a previous squeeze trade
        if cooldown_until is not None and day < cooldown_until:
            continue

        daily_slice = daily_full[daily_full["timestamp"] <= day]
        if len(daily_slice) < 140:  # need enough history for squeeze detection
            continue

        h4_slice = h4_full[h4_full["timestamp"] <= day]
        h1_slice = h1_full[h1_full["timestamp"] <= day]

        decision = engine.evaluate_squeeze_daily(coin, daily_slice, h4_slice, h1_slice, state)

        # Track squeeze activity
        if decision.stage == "squeeze_daily" and decision.bias != "neutral":
            stats["squeeze_events"] += 1
        if "squeeze active" in decision.reason:
            stats["squeeze_active_days"] += 1

        stats["stage_counts"][decision.stage] = stats["stage_counts"].get(decision.stage, 0) + 1

        if decision.signal is None:
            continue

        sig = decision.signal
        entry = float(sig.metadata["entry_price"])
        sl = float(sig.invalidation)
        targets = [float(t) for t in sig.targets]
        direction = sig.direction.value

        forward = h1_full[
            (h1_full["timestamp"] > day)
            & (h1_full["timestamp"] <= day + pd.Timedelta(days=14))
        ]
        outcome, pnl_r = _resolve_zc_outcome(forward, direction, entry, sl, targets)
        stats["fired"] += 1
        stats["total_r"] += pnl_r
        if outcome == "correct":
            stats["correct"] += 1
        elif outcome == "incorrect":
            stats["incorrect"] += 1
        else:
            stats["unresolved"] += 1

        sq_diag = sig.metadata.get("squeeze_diagnostic", {})
        stats["trades"].append({
            "date": str(day.date()),
            "direction": direction,
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "targets": [round(t, 2) for t in targets],
            "outcome": outcome,
            "pnl_r": round(pnl_r, 2),
            "bias_source": "squeeze_daily",
            "squeeze_streak": sq_diag.get("squeeze_streak"),
            "bb_width_mean": sq_diag.get("bb_width_mean"),
        })

        # Cooldown: don't evaluate squeeze for 14 days after a trade
        cooldown_until = day + pd.Timedelta(days=14)

    return stats


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description="N6 squeeze daily A/B backtest")
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH"])
    args = parser.parse_args()

    btc_cot_history = load_cached_cot_history("BTC")

    def _cot_provider(_coin: str, _as_of: pd.Timestamp) -> pd.DataFrame:
        return btc_cot_history

    engine = TheoryV2Engine(cot_history_fn=_cot_provider)
    print(
        f"Loaded BTC COT history: {len(btc_cot_history)} rows "
        f"({btc_cot_history['report_date'].min() if not btc_cot_history.empty else 'n/a'} "
        f"→ {btc_cot_history['report_date'].max() if not btc_cot_history.empty else 'n/a'})"
    )

    results: dict[str, dict[str, Any]] = {}

    # Pooled accumulators
    pooled_control = {coin: {"fired": 0, "correct": 0, "incorrect": 0, "unresolved": 0, "total_r": 0.0}
                      for coin in args.coins}
    pooled_treatment = {coin: {"fired": 0, "correct": 0, "incorrect": 0, "unresolved": 0, "total_r": 0.0}
                        for coin in args.coins}
    pooled_squeeze = {coin: {"fired": 0, "correct": 0, "incorrect": 0, "unresolved": 0, "total_r": 0.0}
                      for coin in args.coins}

    for period, (s_str, e_str) in PERIODS.items():
        start = pd.Timestamp(s_str, tz="UTC")
        end = pd.Timestamp(e_str, tz="UTC")
        results[period] = {}

        for coin in args.coins:
            # Control: weekly path only
            control = _walk_weekly(coin, start, end, engine)
            # Treatment additions: daily squeeze path
            squeeze = _walk_daily_squeeze(coin, start, end, engine)

            if control.get("skipped") or squeeze.get("skipped"):
                reason = control.get("reason") or squeeze.get("reason", "unknown")
                print(f"[{period}] {coin}: SKIPPED — {reason[:80]}")
                results[period][coin] = {"control": control, "squeeze": squeeze, "skipped": True}
                continue

            # Treatment = control + squeeze additions
            treatment = {
                "fired": control["fired"] + squeeze["fired"],
                "correct": control["correct"] + squeeze["correct"],
                "incorrect": control["incorrect"] + squeeze["incorrect"],
                "unresolved": control["unresolved"] + squeeze["unresolved"],
                "total_r": control["total_r"] + squeeze["total_r"],
            }

            results[period][coin] = {
                "control": control,
                "squeeze": squeeze,
                "treatment": treatment,
            }

            # Pool
            for k in ("fired", "correct", "incorrect", "unresolved", "total_r"):
                pooled_control[coin][k] += control.get(k, 0)
                pooled_squeeze[coin][k] += squeeze.get(k, 0)
                pooled_treatment[coin][k] += treatment.get(k, 0)

            ctrl_wr = control["correct"] / (control["correct"] + control["incorrect"]) if (control["correct"] + control["incorrect"]) > 0 else 0
            sq_wr = squeeze["correct"] / (squeeze["correct"] + squeeze["incorrect"]) if (squeeze["correct"] + squeeze["incorrect"]) > 0 else 0
            print(
                f"[{period}] {coin}: "
                f"control={control['fired']}fired/{control['total_r']:+.2f}R "
                f"squeeze={squeeze['fired']}fired/{squeeze['total_r']:+.2f}R "
                f"(WR ctrl={ctrl_wr*100:.0f}% sq={sq_wr*100:.0f}%)"
            )

    # Pooled summary
    print("\n" + "=" * 70)
    print("POOLED RESULTS")
    print("=" * 70)

    for coin in args.coins:
        ctrl = pooled_control[coin]
        sq = pooled_squeeze[coin]
        treat = pooled_treatment[coin]

        ctrl_resolved = ctrl["correct"] + ctrl["incorrect"]
        sq_resolved = sq["correct"] + sq["incorrect"]
        treat_resolved = treat["correct"] + treat["incorrect"]

        ctrl_wr = ctrl["correct"] / ctrl_resolved if ctrl_resolved else 0
        sq_wr = sq["correct"] / sq_resolved if sq_resolved else 0
        treat_wr = treat["correct"] / treat_resolved if treat_resolved else 0

        print(f"\n--- {coin} ---")
        print(f"  Control   : fired={ctrl['fired']:3d}  WR={ctrl_wr*100:5.1f}%  totalR={ctrl['total_r']:+.2f}  "
              f"win={ctrl['correct']} loss={ctrl['incorrect']} unr={ctrl['unresolved']}")
        print(f"  Squeeze   : fired={sq['fired']:3d}  WR={sq_wr*100:5.1f}%  totalR={sq['total_r']:+.2f}  "
              f"win={sq['correct']} loss={sq['incorrect']} unr={sq['unresolved']}")
        print(f"  Treatment : fired={treat['fired']:3d}  WR={treat_wr*100:5.1f}%  totalR={treat['total_r']:+.2f}  "
              f"win={treat['correct']} loss={treat['incorrect']} unr={treat['unresolved']}")
        print(f"  Delta R   : {treat['total_r'] - ctrl['total_r']:+.2f}  "
              f"Delta fired: {treat['fired'] - ctrl['fired']:+d}")

        # Squeeze-specific metrics
        if sq_resolved > 0:
            sq_fp = sq["incorrect"] / sq_resolved
            print(f"  Squeeze FP rate: {sq_fp*100:.1f}% (threshold: ≤20%)")
        else:
            print(f"  Squeeze FP rate: n/a (no resolved squeeze trades)")

    # Hash after the walkers have loaded/fetched their inputs.  Fetches may
    # populate the project cache during the run; taking this snapshot before
    # the walk would record those files as absent and fail to identify the
    # actual evidence inputs.
    input_snapshot = _input_snapshot(args.coins)

    # Acceptance criteria check
    print("\n" + "=" * 70)
    print("ACCEPTANCE CRITERIA")
    print("=" * 70)

    # The pre-registered acceptance criteria are BTC-only.  ETH is reported
    # above as a secondary diagnostic and must not be mixed into these gates.
    coin = "BTC"
    ctrl = pooled_control[coin]
    treat = pooled_treatment[coin]
    sq = pooled_squeeze[coin]
    sq_resolved = sq["correct"] + sq["incorrect"]

    criteria = []

    # 1. BTC treatment R >= 27.69
    c1 = treat["total_r"] >= 27.69
    criteria.append(("BTC treatment R >= 27.69", c1, f"{treat['total_r']:+.2f}"))

    # 2. WR squeeze trades >= 70%
    sq_wr = sq["correct"] / sq_resolved if sq_resolved else 0
    c2 = sq_wr >= 0.70 if sq_resolved > 0 else False
    criteria.append(("BTC WR squeeze trades >= 70%", c2, f"{sq_wr*100:.1f}% ({sq['correct']}/{sq_resolved})"))

    # 3. FP rate squeeze trades <= 20%
    sq_fp = sq["incorrect"] / sq_resolved if sq_resolved else 0
    c3 = sq_fp <= 0.20 if sq_resolved > 0 else False
    criteria.append(("BTC FP rate squeeze trades <= 20%", c3, f"{sq_fp*100:.1f}%"))

    # Rally 63k→78k is reported as a diagnostic only.  Trade existence is not
    # evidence of capture: the OOS trade may still lose or fail to cover the
    # move.  It must not affect the verdict.
    oos_sq = results.get("2026-OOS", {}).get(coin, {}).get("squeeze", {})
    oos_squeeze_trades = [t for t in oos_sq.get("trades", []) if t.get("bias_source") == "squeeze_daily"]
    oos_capture_diagnostic = {
        "trade_count": len(oos_squeeze_trades),
        "trades": oos_squeeze_trades,
        "note": "Diagnostic only; trade existence does not establish profitable capture.",
    }

    # 4. No degradation of existing trades
    c4 = treat["total_r"] >= ctrl["total_r"]
    criteria.append(("BTC no degradation of existing trades", c4, f"control={ctrl['total_r']:+.2f} treatment={treat['total_r']:+.2f}"))

    all_pass = True
    for name, passed, value in criteria:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  [{status}] {name}: {value}")

    verdict = "ACCEPT" if all_pass else "REJECT"
    print(f"\nVERDICT: {verdict}")

    # Write output
    out_dir = PROJECT_ROOT / "docs" / "analysis" / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"squeeze_daily_validation_{ts}.json"
    output = {
        "experiment": "N6_squeeze_daily",
        "input_snapshot": input_snapshot,
        "per_period": results,
        "pooled": {
            "control": pooled_control,
            "squeeze": pooled_squeeze,
            "treatment": pooled_treatment,
        },
        "acceptance_criteria": [
            {"name": name, "passed": passed, "value": value}
            for name, passed, value in criteria
        ],
        "oos_capture_diagnostic": oos_capture_diagnostic,
        "verdict": verdict,
    }
    out_path.write_text(json.dumps(output, indent=2, default=str))
    print(f"\nWrote {out_path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
