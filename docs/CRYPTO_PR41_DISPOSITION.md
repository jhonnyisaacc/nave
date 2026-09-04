# Crypto PR #41 disposition

PR #44 is the canonical crypto futures implementation. PR #41 is not to be
merged separately; its valuable current-universe discovery work is represented
by the existing `MomentumMarketService.scan_current_universe_discovery` path.

| PR #41 functionality | Disposition in #44 | Evidence |
| --- | --- | --- |
| Current market-cap top-100 discovery | PRESERVED_IN_44 | `CurrentUniverseProvider` plus CoinGecko current markets input |
| Liquid perpetual join and identity-safe matching | IMPROVED_IN_44 | Hyperliquid metadata join, canonical identity, unresolved-symbol warnings |
| Current momentum ranking | PRESERVED_IN_44 | Existing momentum discovery ranker and setup validator |
| Derivatives, funding, open-interest, spread and slippage context | IMPROVED_IN_44 | Hyperliquid candles/metadata/order book inputs |
| Point-in-time replay | PRESERVED_IN_44 | Fixture/replay mode remains separate from LIVE mode |
| No-setup funnel and missingness visibility | IMPROVED_IN_44 | NAVE result contract and explicit funnel stages |
| Missed-move audit | IMPROVED_IN_44 | `crypto.futures.missed_moves` keeps later outcomes outside the scan |
| Old command's default live-discovery append | OBSOLETE | Discovery is opt-in on the legacy `crypto scan` commands |
| Any experimental one-off fixture behavior | EXPERIMENT_ONLY | Retained only where deterministic replay tests require it |
| Unrepresented #41 functionality after comparison | CHERRY_PICK_REQUIRED | None; the equivalence check found no valuable current/live behavior requiring a separate cherry-pick |

No cherry-pick from #41 is required after this equivalence check. #41 should be
recorded as superseded by #44 and must not be merged alongside it.
