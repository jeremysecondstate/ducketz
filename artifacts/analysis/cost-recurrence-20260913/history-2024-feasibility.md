# 2024 training-history feasibility and Databento access evidence

Read-only metadata and saved-data audit on September 13, 2026 UTC. **No market-data download, paid request, subscription change, source switch, retraining, or production archive update was performed.** Exact requests, source hashes, provider responses and timestamps are in [history-2024-feasibility.json](history-2024-feasibility.json). The reproducible metadata-only script is [preflight_history_2024.py](preflight_history_2024.py).

## Conclusion

Extending the Gameplan's compatible minute target-price archive into 2024 is feasible for AAPL, AMZN, COST, GOOG, MU and NVDA under the currently selected **XNAS.ITCH** source. Native metadata quoted **$0 total**, **1,128,482 minute records**, and **63,194,992 estimated uncompressed billable bytes** for the missing interval. Existing saved samples already contain core technical features on all 252 trading sessions of 2024 for each of those six stocks. SNDK has neither saved 2024 features nor a native symbol mapping in the requested period; native XNAS.ITCH mapping begins February 24, 2025.

This is a useful next extension once the historical source-selection repair is validated. More history offers additional market conditions and examples; it does not by itself establish better predictive accuracy. A chronological comparison using the same recent holdout, source definitions, boundary-price rules, costs and feature availability should decide whether the longer fit improves calibration and forecast quality. Preserve calibration/assessment separation and purge overlapping target windows. Do not count many overlapping intraday rows as independent trading days.

Historical feature availability, target-label availability, and current observed actual prices are distinct. Adding 2024 improves the pool of potential training examples. It cannot supply a missing observed COST price at a September 2026 evaluation boundary.

## What is already on disk

The immutable Gameplan's selected XNAS.ITCH minute archive currently begins January 13, 2025 (SNDK February 24). The saved upstream Loop B sample file contains earlier hourly and daily features; this audit counted rows by `information_available_at` in America/Los_Angeles, calendar year 2024. Every hourly row counted below has at least 20 non-null core technical features. Counts are potential source inputs, not a claim that every row has an admissible independent target label or complete optional enrichment.

| Symbol | 2024 hourly sample rows | Distinct source dates | 2024 daily sample rows | 2024 samples across all horizons |
|---|---:|---:|---:|---:|
| AAPL | 6,128 | 252 | 252 | 10,785 |
| AMZN | 6,094 | 252 | 252 | 10,723 |
| COST | 5,167 | 252 | 252 | 9,245 |
| GOOG | 5,853 | 252 | 252 | 10,310 |
| MU | 5,896 | 252 | 252 | 10,389 |
| NVDA | 6,146 | 252 | 252 | 10,821 |
| SNDK | 0 | 0 | 0 | 0 |

Saved sample: `C:/DATASTORE/ml/runs/20260912T045017.509319Z/samples.parquet`. Its SHA-256 is recorded in the JSON. This audit leaves that file and all existing Gameplan artifacts unchanged. See [training-history-audit.md](training-history-audit.md) for the actual immutable train, selection, calibration and assessment counts. The current source-selection repair is a separate implementation and is not assumed complete by this feasibility report.

## Exact missing-history request and preflight

- Dataset `XNAS.ITCH`; schema `ohlcv-1m`; input symbology `raw_symbol`.
- Start inclusive: **2023-12-29 00:00 UTC**. The last 2023 trading session is included to supply a possible prior-close reference for the first 2024 trading session.
- End exclusive: **2025-01-13 00:00 UTC**, adjoining the already stored history.
- Native dataset metadata reports the schema available from May 1, 2018 through September 12, 2026, encompassing this interval.
- Native symbology maps the six older stocks throughout the exact requested interval. It reports `SNDK` as `not_found` with no mapping. A metadata-only quote for that absent symbol was rejected; the final preflight records it as unavailable and excludes it from acquisition scope. A follow-up native symbology read confirms SNDK mappings begin February 24, 2025.
- Provider conditions contain no degraded or unavailable dates in the requested interval. This metadata result is not a substitute for checking downloaded records and manifests during a future acquisition.

| Symbol | Estimated records | Estimated billable bytes | Exact quoted cost |
|---|---:|---:|---:|
| AAPL | 208,780 | 11,691,680 | $0.00 |
| AMZN | 198,309 | 11,105,304 | $0.00 |
| COST | 124,730 | 6,984,880 | $0.00 |
| GOOG | 181,047 | 10,138,632 | $0.00 |
| MU | 177,203 | 9,923,368 | $0.00 |
| NVDA | 238,413 | 13,351,128 | $0.00 |
| SNDK | Unmapped | Not applicable | Not quoted |
| **Six-symbol total** | **1,128,482** | **63,194,992** | **$0.00** |

Datastore free-space preflight passed: **1,416,789,389,312 bytes available**, against the repository's required **5,495,099,104 bytes** (5 GiB reserve plus twice the total estimated billable bytes). Billable bytes are uncompressed binary estimates, not compressed download size or final DBN-plus-Parquet usage. Recheck cost, capacity and native availability immediately before any future acquisition, retain the provider-native records, verify their metadata/normalization, and update source history through its native archive workflow. This report does not authorize or perform that acquisition.

## What the Standard-plan document establishes

The repository's [Databento Standard plan inventory](../../../docs/databento-plan/databento_standard_plan_data_access.md), transcribed from the user's August 15, 2026 screenshots, lists **8+ years of US-equity L0 history**, including `ohlcv-1m`. The shorter configured cold-start fetch windows are local bootstrap choices, not the plan's maximum historical entitlement. The live screenshot in that document is specifically labeled **Databento US Equities Mini**. It therefore does not independently establish live XNAS.BASIC entitlement.

The fresh XNAS.ITCH metadata quotes above support zero-dollar access to this exact historical backfill under the signed-in account's existing API credential. A cost quote is bounded to the requested source, schema and period; it is not a permanent promise about other future requests.

## Direct evidence for XNAS.BASIC historical access

The earlier diagnostic did more than query a price quote: **all seven XNAS.BASIC `ohlcv-1m` historical downloads completed successfully**, for March 20 through September 11, 2026, totaling **781,647 native rows**. Each exact quote was $0. This audit rechecked the saved request metadata and SHA-256/size of every listed raw DBN, normalized Parquet, preflight and native-metadata payload. All 28 payload checks passed.

These receipts establish that the existing account/API credential successfully accessed those historical XNAS.BASIC windows. They do **not** establish live XNAS.BASIC permission, exchange licensing, redistribution rights or all other dates/schemas. The logged-in portal is the appropriate additional evidence for the current subscription and live-license distinction. No live client was opened by this audit.

Receipts: [validation-120/validation-manifest.json](source-probe/validation-120/validation-manifest.json); [coverage-review.md](source-probe/validation-120/coverage-review.md). The coverage probe retains an unresolved native zero-volume-bar issue and an August 31 provider quality flag; its conservative positive-volume/date-exclusion comparison is evidence of improved coverage, not automatic approval to replace the production source.
