# Current CME advisory audit

Observed **2026-09-16 04:23:11 UTC** (September 15 21:23:11 Pacific) in Loop A cycle `20260916T040758.032086Z-pid29216`, still `WRITING` at this observation.

The current diagnostic saved at **04:21:20.271292 UTC** has severity **`advisory`**, type `CmeCrossAssetQualityError`, `input_policy=persisted_rows_only` and `provider_rows_preserved=true`. It rejects the September 3 21:00 UTC candidate because NQ BBO is **12 days 07:21:21.164287413 old**, exceeding the unchanged **15-minute** limit.

This is the same optional source-selection limitation documented in the prior-day audit: the reader uses existing partitioned context sources before flat normalized files. All three partition inventories are unchanged from September 15's audit: OHLCV 18 files, BBO 290 and MBP-10 295. The latest MBP partition remains September 3 20:00 UTC, and the diagnostic still selects the same September 3 common-hour candidate. The previous diagnostic rejected that candidate at 11 days 07:19:52 old; the changed age is not a new provider failure.

All **six current CME requests completed successfully on their first attempt**. Current normalized rows match the request log counts:

| Scope | OHLCV rows | BBO rows | MBP-10 rows |
|---|---:|---:|---:|
| Context | 4,941 | 300 | 4,319 |
| Contracts | 4,058 | 240 | 3,595 |

Current OHLCV observations extend through September 16 03:59 UTC; context/contract BBO through approximately 04:08 UTC. MBP requests retain their advertised September 15 19:49:54–20:14:54 UTC window. Every current request has zero `request_limit_saturated` rows; no current-cycle CME error metadata was found. These bounded checks establish receipt/request success, not exhaustive book coverage or a qualified derived context.

**No new actionable data/source defect was identified that justifies an overnight repair or retry solely from this advisory.** Preserve the optional context rejection and its quality gate; do not substitute stale or differently sourced prices. Overall Loop A completion and downstream provider/model qualification remain the root supervisor's checks.

Only local artifacts, source-reader logic and logs were read. No provider calls, process operations, supervision claims, tests or production changes were made.

Evidence: [current JSON](C:/dev/ducketz/artifacts/analysis/overnight-20260916/cme-advisory-audit.json), [current Loop A log](C:/DATASTORE/ml/overnight-runs/20260916T040757.217937Z/loop_a_close_fetch.log), [prior-day advisory audit](C:/dev/ducketz/artifacts/analysis/overnight-20260915/loop-a-provider-advisory-audit.md), [source-selection implementation](C:/dev/ducketz/datafetching/cme_cross_asset_context.py:447).
