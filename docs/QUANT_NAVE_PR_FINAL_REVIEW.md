# Quant → NAVE final repair review

This is the final repair-pass disposition for the existing topic PRs. The
branches stay segmented; the combined integration branch is only for the
one-time full-suite check. `READY_TO_MERGE` describes reviewable code, while
`PRODUCTION_CAPABLE` requires a normal command path with runtime providers and
does not imply scheduling or execution permission.

| PR | Topic | Ready | Remaining blocker | Focused tests | Production capable |
| --- | --- | --- | --- | --- | --- |
| #42 | Foundation | READY_TO_MERGE | Human review; final head includes timezone fix `e5288002c74a326dd67a6f828a3f361893c2954f` plus clean JSON diagnostics `4fbd8cf` | Foundation contract/CLI tests | N/A — shared contract |
| #43 | Cava intelligence | READY_TO_MERGE | Supadata/OpenBB/FRED runtime availability; no job enabled | `tests/test_cava_intelligence.py` | YES, runtime-key/provider gated |
| #44 | Crypto futures + COT | READY_TO_MERGE | Review live provider freshness and keep PR #41 superseded | crypto futures and momentum CLI tests | YES, truthful LIVE/REPLAY paths |
| #45 | Portfolio / ISM / watch | READY_TO_MERGE | Review local portfolio state and provider freshness | portfolio research/provider tests | YES, with provider fallbacks |
| #46 | Political disclosures | READY_TO_MERGE | Official indexes can be delayed; partial evidence is explicit | disclosure normalization/provider tests | YES, truthful partial Congress/Executive support |
| #47 | Memecoin / Dune | READY_TO_MERGE | Discovery remains snapshot/materialization driven; no per-scan remote Dune query | memecoin workflow/materializer tests | NO — research acquisition adapter only |
| #48 | Options | READY_TO_MERGE | Experimental strategy state; no recurring scheduling | options research tests | NO — research-only |
| #49 | Shorts / orchestration | READY_TO_MERGE | Quant job migration remains disabled and requires combined integration review | shorts/orchestration tests | NO — disabled orchestration sidecar |

## Provider boundary

OpenBB is preferred for FRED macro/index series, CFTC COT, and equity history
where the repository adapter exposes the endpoint. Direct official endpoints
are bounded fallbacks or are used for source families OpenBB does not expose in
the required shape. In particular, ISM industry rankings are prose in the
official release; they are not replaced with a guessed index series.

## Safety and operations

- Every declared job remains `enabled: false` and `production_ready: false` in
  `ops/quant_nave_jobs.json` until a separate operational certification.
- Recurring Argentina-local schedules use `America/Argentina/Buenos_Aires`;
  Cava is `18:00` Argentina time.
- Options remain experimental and unscheduled.
- No Abi/Hermes live replacement jobs were enabled or changed in this pass.
- PR #41 is superseded by #44 after the disposition in
  `docs/CRYPTO_PR41_DISPOSITION.md`; it is not merged alongside #44.
- PR #38 remains a rejected isolated experiment and is not incorporated.
- The repository has no intentional GitHub Actions test workflow in this
  series; focused and integration tests are VPS-local and are not represented
  as GitHub CI checks.

## Integration check

The final combined branch is created only for the practical full suite and
CLI/security checks. It is not a replacement for the topic PRs and does not
enable recurring jobs.

The initial research watch universe is recorded in
`config/portfolio_watchlist.json`: AAPL, AMAT, CAT, JPM, JNJ, KO, NKE, GOOGL,
XOM, FCX, NEE, and PLD. These are observation candidates, not buy signals or
trade instructions.

The combined integration run completed with `784 passed, 1 deselected`.
