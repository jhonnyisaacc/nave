# Crypto universe momentum discovery

Status: research-only implementation; no edge is validated.

This layer extends the existing BTC/ETH momentum scan with a reusable,
point-in-time discovery path for a top-100-plus-liquid-perpetual universe. The
existing scan can append the current discovery view with
`--include-universe-discovery`; historical replay remains explicitly offline.
The layer is separate from alerts, wallet code, and execution.

## Hypothesis

> At each historical observation time, a point-in-time universe composed of the
> top 100 crypto assets by available market capitalization plus sufficiently
> liquid perpetual futures can rank emerging relative-strength and momentum
> leaders early enough to produce a realistic, risk-controlled paper-trading
> setup after fees, funding, and slippage.

The implementation labels evidence explicitly:

- **FACT:** observed fixture/provider rows, contract metadata, point-in-time
  membership, and calculated returns/indicators.
- **INFERENCE:** a candidate ranked by the configured composite, passed the
  liquidity gate, or passed the optional existing momentum setup validator.
- **HYPOTHESIS:** the discovery layer may improve opportunity coverage.
- **UNKNOWN:** unavailable market-cap snapshots, OHLCV, open interest,
  funding, liquidation, contract history, venue data, ambiguous identity, or
  incomplete forward outcomes. Unknowns are never counted as favorable
  rejections.

## Point-in-time contract

`FixtureUniverseProvider` implements the provider abstraction used by the
replay. `CurrentUniverseProvider` implements the same contract for one current
market-cap publication joined to current perpetual metadata. A snapshot is
eligible only when both its observation timestamp and `available_at` are no
later than the requested observation time. Historical replay does not fall
back to the current top 100; a production historical provider must use
archived market-cap publications.

Members retain ticker, canonical asset ID or contract address, venue, contract
symbol, quote currency, observation/source/availability timestamps, source,
completeness, rank, and missingness reason. Deduplication is by canonical asset
identity or contract address; ticker-only records remain separate so ambiguous
symbols such as EDGE cannot be silently merged.

The second universe component is `universe_source: liquid_perpetual`. It is
kept even when the asset is outside the historical top 100, and the target
audit reports `OUTSIDE_HISTORICAL_TOP_100` separately from liquidity or data
availability.

## Features and constraints

The configured pipeline calculates 1h, 4h, 24h, 3d, and 7d returns; BTC, ETH,
and configurable median/mean-universe relative strength; return acceleration; range breakout;
volume expansion; trend persistence; 1H/4H structure; higher-high/higher-low
state; swing-high distance; pullback/retest state; ATR; and available
derivatives context (open interest, funding, liquidations, contract volume,
basis, spread, and slippage). The universe benchmark is configurable as the
median or mean 7-day return of the point-in-time observed members.

Thresholds live in `trading/crypto/momentum/discovery_defaults.json` and are
loaded into one `DiscoveryConfig` object:

| Constraint | Default |
| --- | ---: |
| Minimum 24h quote volume | 5,000,000 |
| Minimum open interest | 1,000,000 |
| Maximum spread | 20 bps |
| Maximum estimated slippage | 25 bps |
| Minimum trading history | 72 hours |
| Minimum rank score | 50/100 |
| Universe benchmark | Median 7-day member return |
| Meaningful forward move | directional 10% over 168 hours |
| Fee assumption | 5 bps per side |
| Default slippage assumption | 10 bps per side |

Missing liquidity fields produce `UNKNOWN`; known threshold failures produce a
liquidity rejection. A perpetual contract, sufficient history, and all
required liquidity fields are required before a candidate is eligible.

## Setup and outcome separation

Discovery produces ranking observations only. Optional setup validation is an
adapter around the existing strict `MomentumSetupEngine` and emits only the
approved research classifications, including `PROMISING EXPLORATORY SIGNAL`,
`WEAK / UNSTABLE SIGNAL`, `INSUFFICIENT DATA`, and `BLOCKED BY OUTCOME COVERAGE`.
It requires 4H structure, a 1H trigger, entry zone, invalidation, TP1/TP2/TP3,
expected move, net cost-aware R:R, liquidity, and no-chase checks.

Paper simulation uses the first future candle that actually trades through the
entry zone, conservative stop-first handling when a candle touches both stop
and target, observed spread/slippage when available, fees, funding, and holding
period. It records `NO_FILL` when the entry zone is never reached and never
submits an order or creates a live watch.

## Reproducible offline replay

The checked-in fixture is synthetic and is not market evidence. It exists to
test contracts, replay determinism, and missingness handling:

```bash
PYTHONPATH=. .venv/bin/python cli/main.py crypto universe-momentum-scan \
  --fixture tests/fixtures/crypto_momentum_replay.json \
  --universe-size 100 \
  --symbols ARB,CAKE,CRV,TWT,EDGE,PONS \
  --start 2026-08-25T00:00:00Z \
  --end 2026-09-01T00:00:00Z \
  --cadence 6h \
  --no-sensitivity \
  --no-validate-setups \
  --json
```

The output schema is `crypto-momentum-discovery-replay.v1` and includes source
metadata, all observations, first-eligible timestamps, forward outcomes,
coverage/precision/false positives, MFE/MAE, paper expectancy/drawdown/turnover,
fee/funding/slippage impact, asset/regime summaries, cadence/threshold
sensitivity, and the explicit target audit. On this fixture, EDGE is purposely
ambiguous and PONS is purposely outside the historical top 100; neither state
is interpreted as a successful or unsuccessful trading result.

For a current research pass through the existing BTC/ETH scan:

```bash
PYTHONPATH=. .venv/bin/python cli/main.py crypto momentum-scan \
  --include-universe-discovery --universe-size 100 --json
```

The current pass uses CoinGecko market-cap ordering and Hyperliquid perpetual
metadata. Provider failures remain `PROVIDER_UNAVAILABLE`; missing order-book
depth is not turned into a favorable liquidity assumption.

For real historical use, supply a provider with archived market-cap snapshots
and venue/contract history. If that data is unavailable, the result must stay
`PROVIDER_UNAVAILABLE`, `INCOMPLETE_DATA`, or another explicit unknown state;
the current top 100 must not be substituted.
