# Quant → NAVE topic-scoped PR plan

The series is intentionally stacked only where a shared contract is required.
Each row represents one reviewable research responsibility; `OPEN` means a PR
has been opened and is awaiting review. All workflows remain
`READ_ONLY_RESEARCH_ONLY_HUMAN_GATED`.

| PR | Topic | Branch | Base | Depends on | Status | Tests | URL |
| -- | -- | -- | -- | -- | -- | -- | -- |
| 1 | NAVE research foundation | `feat/nave-research-foundation` | `main` | — | OPEN | 8 passed | [PR #42](https://github.com/jhonnyisaacc/nave/pull/42) |
| 2 | José Luis Cava / macro intelligence | `feat/nave-cava-intelligence` | PR 1 | PR 1 | OPEN | 14 passed | [PR #43](https://github.com/jhonnyisaacc/nave/pull/43) |
| 3 | Crypto futures momentum + COT | `feat/nave-crypto-futures` | PR 1 | PR 1 | OPEN | 47 passed | [PR #44](https://github.com/jhonnyisaacc/nave/pull/44) |
| 4 | Portfolio / ISM / watchlist | `feat/nave-portfolio` | PR 1 | PR 1 | OPEN | 32 passed | [PR #45](https://github.com/jhonnyisaacc/nave/pull/45) |
| 5 | Political financial disclosures | `feat/nave-political-disclosures` | PR 1 | PR 1 | OPEN | 35 passed | [PR #46](https://github.com/jhonnyisaacc/nave/pull/46) |
| 6 | Memecoin research + Dune | `feat/nave-memecoin-research` | PR 1 | PR 1 | OPEN | 41 passed | [PR #47](https://github.com/jhonnyisaacc/nave/pull/47) |
| 7 | Options research | `feat/nave-options-research` | PR 1 | PR 1 | OPEN | 14 passed | [PR #48](https://github.com/jhonnyisaacc/nave/pull/48) |
| 8 | Stock shorts + Quant orchestration | `feat/nave-shorts-and-quant-orchestration` | PR 1 / `main` | PR 1; topic readiness | OPEN | 13 focused; 711 passed, 1 skipped full | [PR #49](https://github.com/jhonnyisaacc/nave/pull/49) |

## FOLLOW_UP_TOPIC

- Supadata credentials/cursor ownership remains outside public Git; PR 2 must
  use the runtime provider and preserve the cursor on transcript failure.
- Abi/Hermes job declarations are inspected in `/home/david/agent` but are not
  changed by the foundation PR.
- PRs 2–8 are independent topic branches based on PR 1; PR 8 is logically
  last for Quant migration but does not artificially include the other topic
  implementations.
- Supadata is the configured runtime transcript provider for Cava when
  `SUPADATA_API_KEY` or `SUPADATA_API_TOKEN` is present; no credential is
  stored in this repository.
