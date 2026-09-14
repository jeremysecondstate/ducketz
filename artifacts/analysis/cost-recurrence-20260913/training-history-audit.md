# Gameplan training-history audit

Read-only audit of Monday September 14's immutable Gameplan, performed September 12 Pacific / September 13 UTC. No training, source-policy change, cohort rewrite, pipeline action, or provider request was performed. The JSON companion contains exact UTC decision timestamps and per-partition counts.

## What the published predictions contain

The saved Gameplan has **168 forecasts: 24 for each of seven symbols**, including COST. Both raw and calibrated probabilities are present in all 168 rows. Thus the seven missing COST actuals are missing observed evaluation endpoints, not missing predictions and not a shortage of model outputs. More old history cannot supply an absent price at a particular September 11 boundary.

## Years in DATASTORE versus the history admitted here

The saved Loop B samples contain hourly and daily feature histories extending into **2023** for six symbols (SNDK starts in 2025). The current market-data catalog records native EQUS.MINI hourly and daily stock bars from March 28, 2023 for COST and the other five older symbols. It also records cold XNAS.ITCH hourly history from August 2021 and daily history from August 2019. These are real older histories, but coarser bars and different feeds are not interchangeable with this Gameplan's exact extended-session minute endpoints.

This Gameplan's selected target-price archive is **XNAS.ITCH one-minute bars, January 13, 2025 through September 11, 2026** (SNDK begins February 24, 2025). COST contributes 208,246 loaded minute bars. This is about 20 months, not several years of minute-level target labels. All available verified partitions for the selected source are loaded; there is no 120-session training cap in this path. The 120 sessions in the source-probe report are the comparison period for that separate coverage audit.

Training also requires a causal feature row and admissible observed target endpoints for the same date. Every aligned target row is retained in the saved training-cohort parquet; the model uses chronological train/selection/calibration/assessment partitions with overlapping outcome windows purged at partition boundaries. The selected estimator is refitted on train plus selection; calibration and assessment stay outside that fit. The holdout size is min(63, max(10, decision-cluster-count // 8)), not a total-history limit.

Dates below are **action dates**. They are deliberately not a claim that the actual model fit includes the later calibration or assessment observations.

| Horizon | Partition | All-symbol action-date span | Rows | COST rows | COST action-date span |
|---|---|---|---:|---:|---|
| 1h | train | 2025-01-13 – 2026-03-19 | 16,614 | 1,322 | 2025-01-13 – 2025-12-26 |
| 1h | selection | 2026-03-20 – 2026-05-12 | 2,626 | 18 | 2026-04-08 – 2026-05-12 |
| 1h | calibration | 2026-05-13 – 2026-07-09 | 3,314 | 427 | 2026-05-13 – 2026-07-09 |
| 1h | assessment | 2026-07-10 – 2026-09-11 | 3,705 | 428 | 2026-07-10 – 2026-09-11 |
| 4h | train | 2025-01-13 – 2026-03-19 | 4,505 | 367 | 2025-01-13 – 2025-12-26 |
| 4h | selection | 2026-03-20 – 2026-05-12 | 746 | 4 | 2026-04-08 – 2026-05-12 |
| 4h | calibration | 2026-05-13 – 2026-07-09 | 943 | 120 | 2026-05-13 – 2026-07-09 |
| 4h | assessment | 2026-07-10 – 2026-09-11 | 1,053 | 122 | 2026-07-10 – 2026-09-11 |
| 1d | train | 2025-03-04 – 2026-04-17 | 4,263 | 261 | 2025-03-04 – 2026-04-08 |
| 1d | selection | 2026-04-20 – 2026-06-01 | 750 | 28 | 2026-05-12 – 2026-06-01 |
| 1d | calibration | 2026-06-02 – 2026-07-20 | 896 | 92 | 2026-06-02 – 2026-07-16 |
| 1d | assessment | 2026-07-21 – 2026-09-11 | 1,001 | 69 | 2026-07-21 – 2026-09-11 |
| 1w | train | 2025-03-04 – 2026-04-14 | 855 | 53 | 2025-03-04 – 2026-04-08 |
| 1w | selection | 2026-04-21 – 2026-05-26 | 133 | 4 | 2026-05-20 – 2026-05-26 |
| 1w | calibration | 2026-06-02 – 2026-07-13 | 162 | 17 | 2026-06-02 – 2026-07-10 |
| 1w | assessment | 2026-07-20 – 2026-09-04 | 194 | 13 | 2026-07-29 – 2026-09-04 |

The final train-plus-selection fits contain 19,240 / 5,251 / 5,013 / 988 rows for 1h / 4h / 1d / 1w respectively; COST contributes 1,340 / 371 / 289 / 57. Models are pooled across symbols with symbol and route inputs; these are not seven entirely separate models. The before-purge aligned cohort totals are 26,273 / 7,261 / 7,100 / 1,416, with COST 2,196 / 614 / 460 / 92.

## Additional history loss found upstream of target prices

`ml/nightly_gameplan.py:564–589` derives overnight source features only from existing Loop B hourly samples whose next target is exactly 04:00 PT on a later date. That couples independent Gameplan historical feature eligibility to the rolling model's next-target clock. It can discard a prior trading day's causal features when that day lacks a sufficiently late hourly source bar to generate the next-session 04:00 target.

In the 1h/4h model-selection interval (action dates March 20–May 12, 2026), eligible source-day counts were AAPL 32, AMZN 30, GOOG 24, MU 36, NVDA 37, SNDK 28, **COST 2**. Those two COST dates are April 8 and May 12. This loss occurs **before** the independent target-price endpoint filter.

| Month | AAPL | AMZN | GOOG | MU | NVDA | SNDK | COST |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2026-01 | 10 | 7 | 4 | 13 | 20 | 3 | 0 |
| 2026-02 | 12 | 15 | 3 | 12 | 19 | 7 | 0 |
| 2026-03 | 16 | 13 | 12 | 19 | 22 | 15 | 0 |
| 2026-04 | 19 | 18 | 14 | 20 | 21 | 16 | 1 |
| 2026-05 | 17 | 16 | 12 | 20 | 20 | 19 | 13 |
| 2026-06 | 20 | 17 | 18 | 21 | 21 | 21 | 21 |
| 2026-07 | 16 | 15 | 15 | 22 | 22 | 22 | 22 |
| 2026-08 | 19 | 16 | 12 | 21 | 21 | 21 | 21 |

COST's native EQUS.MINI hourly file has 676 bars during January 1–May 10, 2026, including regular-hour bars on 88 sessions, but only one 16:00 PT bar and no 14:00 or 15:00 bars. The saved 1m-derived hourly bridge begins May 11, 2026. Its later completed hourly rows allow the inherited overnight source selector to retain substantially more dates. This explains the source-day gap independently from missing XNAS.ITCH target endpoints; it does not imply that January–April had no market trading or no stored COST history.

## Focused prospective work supported by the evidence

1. Give the independent Gameplan its own calendar-based prior-session feature selection, with explicit causal cutoff, feature availability, and freshness rules. Validate and version this prospectively. Do not silently change historical cohort reconstruction or relax price-label timing to make old rows pass.
2. Extend the target-price minute history far enough to overlap the usable causal feature history, with an explicitly selected feed and source-quality rules. Older daily/hourly bars alone cannot prove a specific five-minute endpoint.
3. Report loaded source span, aligned cohort span, and actual fit/holdout counts per symbol/horizon. This distinguishes having files, using eligible examples, and obtaining current actual prices.

These changes address history utilization. The broader-feed proposal addresses extended-session price coverage. They are separate fixes and should be validated separately.

## Evidence and code

- Immutable Gameplan: `C:/DATASTORE/ml/nightly-gameplan-runs/20260912T051210.050260Z/manifest.json`, `model-reports.json`, `forecasts.parquet`, and `training-cohort-*.parquet`.
- Source samples: `C:/DATASTORE/ml/runs/20260912T045017.509319Z/samples.parquet`.
- Market-data catalog observed September 12, 2026 04:50 UTC: `C:/DATASTORE/catalog/market-data/current.json` (metadata inventory, not a new provider verification).
- Native COST hourly file: `C:/DATASTORE/stocks/COST/bars/1h/databento/normalized/COST_source_1825d_1h_ohlcv-1h_1h.parquet`.
- Derived COST hourly file: `C:/DATASTORE/stocks/COST/bars/1h/databento/normalized/COST_derived_1m_1h.parquet`.
- `ml/nightly_gameplan.py:203–215`: source loading and independent training groups; `564–594`: overnight source selector; `1163`: chronological partitioning; `1515–1565`: holdout sizing and overlap purge.
- `ml/independent_stock_targets.py:144–202`: causal source/price alignment and five-minute endpoint admission; no total-history cap.
- `ml/rolling_samples.py:389–397` and `ml/calendars.py:408–454`: rolling targets after available source information.
- `datafetching/databento_fetch.py:647–691`: coverage-proven sparse hourly derivation from available minute-history range.
