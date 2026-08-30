# Autonomous experiment log

Baseline: pooled +44.14R (BTC +20.79R / 77.8% WR, ETH +23.35R / 78.9% WR).
See `00_baseline.md` for the comparison rules every experiment must beat.

## Convention

- This table is the canonical record. Every experiment — shipped *or* skipped —
  gets a row.
- A standalone `NN_<name>.md` file is **only required for shipped (✅) experiments**
  and for the baseline. Skipped experiments (❌) live as a single row here.
- Skipped experiments still need their row to land on a branch that gets merged
  (e.g., the next shipped experiment, or a cleanup PR). Never lose a row just
  because the branch is discarded.
- The "Pooled R" column is the BTC+ETH (+ shipped extras) total from
  `scripts/theory_v2_backtest.py`. "Δ vs base" is relative to the iter-18
  baseline `+44.14R` until a new baseline is declared in `00_baseline.md`.

| # | Branch | Hypothesis | Pooled R | Δ vs base | Verdict | PR |
|---|---|---|---|---|---|---|
| 01 | experiment/add-solana | SOL as a 3rd coin | +64.82 | +20.68 | ✅ ship | #17 |
| 02 | experiment/add-avax | AVAX as a 3rd coin | +51.63 | +7.49 | ❌ skip — WR collapses to 61.5% | — |
| 03 | experiment/momentum-threshold-tune | min_velocity 1.2 → 1.0/1.3/1.4 | +40.1/+44.1/+43.3 | -4 / 0 / -0.85 | ❌ 1.2 is locally optimal | — |
| 04 | experiment/chase-gate-tune | min_retrace 0.50 → 0.40/0.55/0.60, max → 0.85 | +44.14 | 0 | ❌ insensitive (permissive paths dominate) | — |
| 05 | experiment/atr-floor-tune | atr_floor 1.5 → 1.25/1.3/1.4/1.75 | +49.27/+48.53/+46.72/+43.19 | +5.13/+4.39/+2.58/-0.95 | ❌ pooled WR drops 1-3pp at all wins; not strict | — |
| 06 | (extend forward window 14d → 21d) | resolution window | +44.65 | +0.51 | ❌ noise; ETH WR drops 6pp | — |
| 06b | (trail SL to +0.5R after ZC1 hit) | exit policy | +43.42 | -0.72 | ❌ ETH regresses | — |
| 07 | experiment/add-link | LINK as 4th coin | +67.15 | +23.01 | ✅ ship | #18 |
| 15 | experiment/n6-squeeze-daily | daily-cadence squeeze breakout path (off by default) | +49.70 BTC / +52.43 ETH | diagnostic only | ❌ REJECT — BTC FP 26.1% (6/23 resolved), fresh rerun 2026-08-30; snapshot `squeeze_daily_validation_20260830T001059Z.json`; Draft PR remains off | #38 |
