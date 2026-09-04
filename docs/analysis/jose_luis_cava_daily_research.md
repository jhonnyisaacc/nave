# José Luis Cava daily market research

This document defines the NAVE-side contract for the daily José Luis Cava
video review. The recurring scheduler is owned by Abi/Hermes (Quant), not by
the NAVE repository. NAVE supplies the research methodology, data-quality
rules, provider adapters, point-in-time checks, and evidence conventions.

## Current runtime

- Abi/Hermes job: `1ee0ff3c28bb`, `Jose Luis Cava — análisis diario de mercado`.
- Schedule: daily at `18:00` in the configured `America/Argentina/Buenos_Aires`
  timezone.
- Source channel: `@JoseLuisCavatv`.
- Verified channel ID: `UCvCCLJkQpRg0NdT3zNcI08A`.
- RSS source:
  `https://www.youtube.com/feeds/videos.xml?channel_id=UCvCCLJkQpRg0NdT3zNcI08A`.
- Delivery is a report for human review. It is not an execution workflow.
- The private deduplication cursor is maintained by Abi/Hermes, outside Git.

The canonical NAVE command is now `nave intel cava daily`; Quant should consume
its structured JSON result and render the human-facing report. Transcript
retrieval uses the runtime-configured Supadata provider with the
`SUPADATA_API_KEY` (or `SUPADATA_API_TOKEN`) environment variable. Supadata's
transcript endpoint supports synchronous results and bounded asynchronous job
polling; the API key is never included in result state or reports.

The old `UCfEJZJ4V8e6lQkV8m4r0n5w` identifier is invalid and must not be used.
If RSS is unavailable, the task must record that fact and use an explicit
public-channel or native-transcript fallback; it must never invent a video or
silently treat a missing source as complete.

## Research contract

For each genuinely new video, Abi should:

1. Record the public video ID, title, publication time, retrieval time, and
   transcript source. Deduplicate by video ID.
2. Obtain the transcript through the configured native transcript provider and
   preserve unavailable, truncated, or failed retrieval as an evidence state.
3. Split the speaker's claims into separate FACT, INFERENCE, HYPOTHESIS, and
   UNKNOWN items. Do not turn a narrative into a signal without a mechanism.
4. Identify the affected market or instrument, exact horizon, catalyst and
   activation condition, invalidation, liquidity/cost risks, missing evidence,
   and the next bounded check.
5. Compare claims with NAVE data and primary sources. For any decision-time
   claim, enforce `available_at <= decision_time`; data published later is not
   eligible evidence for that decision.
6. Produce a concise report beginning with `STOCKS:`. Use `ENTER`, `WATCH`,
   `HOLD`, `REVIEW`, `EXIT`, or `NO ACTION / INSUFFICIENT EVIDENCE` only when
   the evidence supports the classification. An incomplete input cannot
   produce an `ENTER` recommendation.

The report must include source URLs and retrieval timestamps. Citation or
evidence verification failures are report-quality failures and must remain
visible; a report is not validated merely because text was generated.

## Bootstrap/test status

The seven-video bootstrap test is preserved in the private Abi/Hermes cursor:
seven prior video IDs were recorded before the first recurring run. The next
run processed the new video `K8JEHgT89gg` on 2026-09-02 and classified the
result as `WATCH`, with no order or other financial action.

That run exposed two operational issues which remain part of the audit trail:
the old RSS identifier returned HTTP 404, and the citation verifier reported
insufficient coverage and missing verbatim evidence quotes. The recurring job
must continue only as read-only research and should treat those checks as
quality gates for future reports.

The 2026-09-03 continuation detected the new copper video through the verified
RSS feed, but the native transcript was unavailable from the VPS. It therefore
returned `NO ACTION / INSUFFICIENT EVIDENCE`, preserved the prior cursor, and
left the next bounded check as transcript recovery. The local/manual execution
recorded a Discord delivery failure; the multiplexed gateway remains the
intended scheduled delivery path, and notification delivery still requires a
successful scheduled-tick verification.

## Action boundary

NAVE and Abi may collect evidence, test hypotheses, and publish human-gated
observations or candidate signals. They must not place orders, sign wallet
transactions, transfer funds, create execution-ready watches automatically,
or claim that a robust edge has been validated from these videos.

## Related NAVE references

- [Current research state](../../research/nave/state.json)
- [Research task template](../hermes/nave-task-template.md)
- [Provider and evidence philosophy](../../README.md#research-philosophy)
- [Current and historical analysis](./)
