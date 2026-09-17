# CME advisory audit — September 17 preparation

Observed at **2026-09-17 04:21:29 UTC** (September 16 21:21:29 Pacific), while Loop A cycle `20260917T040723.264870Z-pid58488` remained `WRITING`.

The 04:19:12 UTC diagnostic repeats the prior optional derivation rejection: `severity=advisory`, `CmeCrossAssetQualityError`, `input_policy=persisted_rows_only`, `provider_rows_preserved=true`. It selects the same September 3 21:00 UTC candidate, rejecting NQ BBO age **13 days 07:19:13.130596413** against the unchanged 15-minute gate. Partition inventories remain unchanged from yesterday: OHLCV 18 files, BBO 290, MBP-10 295. The reader still prefers these partition files over flat normalized sources.

All six current CME requests logged successful receipt on their first outer attempt. Current normalized receipt counts are:

| Scope | OHLCV rows | BBO rows | MBP-10 rows |
|---|---:|---:|---:|
| Context | 2,386 | 300 | 5,000 |
| Contracts | 4,041 | 240 | 5,000 |

**New coverage limitation:** both current MBP captures are limit-saturated, unlike yesterday. They each retain `limit=5000`, `request_limit_saturated=true`, `latest_window_shrink_count=5`, and no empty-window expansion. Their original request range was September 16 19:48:16.428518–20:13:16.428518 UTC; the bounded collector shrank each to **20:12:29.553518–20:13:16.428518 UTC** (46.875 seconds). Context observations end at 20:12:54.750612109; contracts at 20:13:07.633046323. These are explicit capped captures and must not be described as complete book coverage. The existing derived-context gate rejects limit-saturated books independently.

Current OHLCV observations reach September 17 03:59 UTC and BBO approximately 04:08:59 UTC, with no saturated rows in those captures. No current-cycle CME error metadata was found. These are bounded receipt checks, not exhaustive market-book qualification.

The FMP energy diagnostic remains the same retained September 2 advisory: provider quote timestamp exceeds local receipt by **9.471 seconds**, above its **5-second** limit; `provider_rows_preserved=true`. No new diagnostic rows or explicit FMP advisory log lines were present at this observation.

The current CME advisory remains an unavailable optional derived context. Its new capped MBP receipts are material to CME context completeness, but these local facts do not establish a stock-source/publication blocker. **No focused production repair or pipeline restart is indicated by this advisory alone.** Preserve stale and saturation rejection, source data, and acquisition bounds. A source-selection-only change would not make the capped MBP source qualify. Overall Loop A completion and downstream provider/model coverage still require the supervising operator's final checks.

Only local diagnostics, selected normalized metadata columns, partition filenames, code, and logs were inspected. No broker/provider calls, process operations, supervision claims, production changes, or tests were performed.

Evidence: [JSON audit](C:/dev/ducketz/artifacts/analysis/overnight-20260917/cme-advisory-audit.json), [Loop A log](C:/DATASTORE/ml/overnight-runs/20260917T040722.433471Z/loop_a_close_fetch.log), [prior audit](C:/dev/ducketz/artifacts/analysis/overnight-20260916/cme-advisory-audit.json), [source selection](C:/dev/ducketz/datafetching/cme_cross_asset_context.py:444), [saturation rejection](C:/dev/ducketz/datafetching/cme_cross_asset_context.py:1007), [bounded window shrinking](C:/dev/ducketz/app/services/databento_cme_context.py:232).
