# N6 — Daily-cadence squeeze entry (bypass weekly bias gates)

**Verdict: REJECT** (fresh rerun 2026-08-30)

## Problem

NAVE evaluates bias on a weekly cadence, but a volatility squeeze → explosion
completes in 1–2 days. The N5 weekly-bias squeeze detector failed (0% WR,
−7.0R, 100% FP) because by the time the weekly evaluator ran, the squeeze had
already exploded and price was extended — and the downstream chase/climax
gates then rejected the (now-extended) entry.

## What works (from N5 discovery)

The squeeze *pattern* has 94.4% historical precision (34 TP / 2 FP across 36
BTC+ETH events 2017–2026). The problem is evaluation *timing*, not signal
quality.

## Solution — N6 daily-cadence path

An independent `evaluate_squeeze_daily()` path on `TheoryV2Engine`:

1. Each day, evaluate squeeze state (BB width < p25 of 120d OR < 3.5% absolute,
   sustained ≥ 7 days).
2. When squeeze was active AND a breakout bar appears (close beyond compression
   range ± 0.5× ATR-14), arm the bias immediately (do not wait for the weekly
   evaluation).
3. Apply existing daily/4H/1H gates normally, but **SKIP** both the chase gate
   and the climax cooldown — a squeeze breakout *is* the move and *is* the
   climax, so blocking it would prevent the exact entry this path targets.
4. Timeout: no breakout within 14 days of squeeze end → disarm.

The weekly path (`evaluate()`) is untouched. Production defaults are unchanged:
`evaluate_squeeze_daily()` is only invoked explicitly by the A/B harness — it is
**not** wired into live evaluation.

## Files

- `trading/crypto/analysis/squeeze_daily.py` — daily squeeze evaluator (self-contained)
- `trading/crypto/theory_v2.py` — `evaluate_squeeze_daily()` method (additive, off by default)
- `scripts/squeeze_daily_backtest.py` — N6 A/B harness (control vs treatment)
- `docs/analysis/raw/squeeze_daily_validation_*.json` — raw per-trade evidence
- `tests/test_n6_squeeze_daily_isolated.py` — isolation/regression tests
- `tests/test_n6_squeeze_daily_logic.py` — behavioral breakout tests

## Acceptance evidence (pre-registered BTC criteria)

The acceptance gates are BTC-only; ETH is a separately reported diagnostic and
is not pooled into the gates. The fresh rerun used the committed script with Binance REST klines cached
locally during the run, through the 2026-08-26 OOS boundary. Its raw output is
committed as `docs/analysis/raw/squeeze_daily_validation_20260830T001059Z.json`.
The artifact contains an `input_snapshot` manifest with SHA-256 and byte-size
for every BTC/ETH OHLCV cache file and the COT cache used by the run. A rerun
against different cache contents is therefore a different evidence snapshot,
not a silently interchangeable reproduction.

That rerun is **REJECT**: BTC treatment is +49.70R, but the BTC squeeze false
positive rate is 26.1% (6/23 resolved), above the 20% gate. The earlier ACCEPT artifacts
(+35.41R, 15.4% FP) are retained as historical evidence but do not describe
the final head and must not be used for merge or enablement.

| Criterion | Threshold | Result | Pass |
|---|---|---|---|
| BTC treatment R | ≥ 27.69 | +49.70 | ✅ |
| BTC WR squeeze trades | ≥ 70% | 73.9% (17/23 resolved) | ✅ |
| BTC FP rate squeeze trades | ≤ 20% | 26.1% (6/23 resolved) | ❌ |
| Rally 63k→78k captured (OOS 2026) | diagnostic only | 0 trades; no capture evidence | — |
| No degradation of existing trades | YES | control +24.49 → treatment +49.70 | ✅ |

Per-coin:

- BTC (fresh rerun): squeeze +25.22R; control 29 fired/+24.49R → treatment 54 fired/+49.70R.
- ETH (diagnostic only): squeeze +26.33R; control 28 fired/+26.10R → treatment 50 fired/+52.43R.

The focused regression suite contains **44 tests**:
`python -m pytest tests/test_theory_v2.py tests/test_n6_squeeze_daily_logic.py tests/test_n6_squeeze_daily_isolated.py -q` → 44 passed.

The OOS rally observation is retained as a diagnostic, not an acceptance gate:
the presence of a trade does not prove that the strategy captured the move, and
the fresh rerun observed no 2026 OOS squeeze trade. The rerun therefore remains
**REJECT** regardless of that diagnostic.

## Merge path (still required before enabling)

1. Add a `squeeze_daily_config` flag to `TheoryV2Engine` (default OFF).
2. In live evaluation, call `evaluate_squeeze_daily()` on each daily bar only
   when that flag is set.
3. `SqueezeDailyState` must be persisted across daily bars (not recreated per
   evaluation).

This PR is a **Draft** and does not enable the path. Merge and enabling remain
human-gated.
