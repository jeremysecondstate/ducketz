# September 25 Gameplan preparation — complete and verified

**Native workflow COMPLETE; all twelve completion-audit sections VERIFIED with disclosed coverage notes and no errors.** The workflow for source September 24 and action September 25 finished at **2026-09-25 06:22:47 UTC / September 24 23:22:47 Pacific**, completing all eight required stages in 2h 14m 25s. This was before the September 25 03:30 Pacific target and the unchanged **04:00 Pacific / 11:00 UTC deadline**. No exception, restart or resume was used. The [native stage report](C:/DATASTORE/ml/overnight-runs/20260925T040822.112032Z/stage-report.json) and [receipt](C:/DATASTORE/ml/overnight-runs/20260925T040822.112032Z/receipt.json) record zero orders and broker execution disabled.

Read the [September 25 Gameplan](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260925T061845.719846Z/Gameplan.md) and [September 24 results](C:/DATASTORE/ml/gameplan-actuals-review-runs/20260925T062206.766961Z/Gameplan-results.md). The immutable publication is [20260925T060654.631426Z](C:/DATASTORE/ml/nightly-gameplan-runs/20260925T060654.631426Z/receipt.json), pinned by receipt SHA-256 `f70b873f48aa206e3b0be21c179008854aeb012691abb53b8ab9a18d57a01275`.

The production universe is eleven active symbols: AAPL, AMZN, GOOG, MU, NVDA, SNDK, COST, CROX, PATH, TWST and IONQ. Saved outputs contain **264 forecasts, 264 stock-only option intents and 264 augmented planning rows**, exactly 24 per symbol; the options placeholders remain `NO_TRADE_STOCK_ONLY`. No completed source session or onboarding batch was rerun.

| Required stage | Native completion UTC |
|---|---|
| Loop A completed-session fetch | 05:32:50 |
| Loop B Directional generation | 05:57:50 |
| Stock target history | 06:00:37 |
| Cumulative Gameplan evaluation | 06:06:53 |
| Gameplan publication | 06:16:44 |
| Independent sizing enrichment | 06:18:28 |
| Cash/share trade planning | 06:21:49 |
| Prior-session actuals review | 06:22:47 |

**Data and historical admission.** The bounded [provider-stage review](C:/dev/ducketz/artifacts/analysis/overnight-20260925/preflight/provider-stage-review.md) verified all eleven symbol captures and all **33 production OPRA cursors**, through exclusive September 25. Native acquisition reports zero blocking/optional capture failures, 33 successful OPRA scopes and no Live replay. All 33 exact zero-dollar preflights passed capacity checks. Current OPRA rows are 193,412 hourly bars, 14,770,794 minute CBBO records and 40,306 definitions. All 456 distinct logged derived-output paths exist. All 66 current OPRA raw/normalized files passed independent hashing; the [provider audit](C:/dev/ducketz/artifacts/analysis/overnight-20260925/verification/provider-completion.md) has no discrepancies or pending checks.

Loop B reports nine newly fitted models, 340,328 samples and 99 routes. Optional Pricing remains excluded on all routes under unchanged 80% completeness, 80% freshness and twenty-target gates; its 51 compact source files and August 20 authority remain unchanged. Fresh CME capture succeeded after one native retry of a gateway 504, but optional CME context remains excluded because of retained-partition selection, alias, staleness and saturated MBP limits. Current FMP USO proxy input passes its timestamp check, while retained history still contains the prior up-to-9.471-second skew. These optional exclusions were preserved: [Pricing review](C:/dev/ducketz/artifacts/analysis/overnight-20260925/preflight/pricing-gate-review.md), [CME review](C:/dev/ducketz/artifacts/analysis/overnight-20260925/preflight/cme-advisory-review.md).

The native [target-history receipt](C:/DATASTORE/ml/stock-target-history-runs/20260925T055751.479667Z/receipt.json) reports eleven current minute downloads, no failures/no-data results and zero estimated cost. The freshly recomputed feature-history extension required **zero additional prefix requests**: existing verified prefixes were reused, and original cursors were preserved during extension. No license, cross-dataset blend or fabricated training price was introduced. Independent verification passed for the source files, eleven original cursor snapshots, bound semantic values and nonregressing current cursors. The native receipt reports exact cursor preservation; it does not separately bind the entire original-cursor snapshot file by an outer hash.

The manifest-bound [archive report](C:/DATASTORE/ml/nightly-gameplan-runs/20260925T060654.631426Z/archive-history.json) records 17,091 causal feature rows, 70 excluded intervals (53 degraded-quality and 17 undefined-OHLC), 25 warmup/quality resets, 26 known split boundaries and five discontinuity boundaries. Seconds contribute verification only: 201 native partitions and 2,842,334 exact second/minute OHLCV overlaps, with zero added or synthetic training rows. Independent reconstruction reproduced every saved cohort row and all 264 raw/calibrated forecast probabilities exactly. The audit hashed 900 bound source files totaling 948,037,870 bytes and verified normalized second/minute OHLCV consistency plus native DBN request headers. It did not replay every raw DBN record.

| Symbol | First native daily/minute date | Last native minute start, September 24 UTC | Causal feature rows |
|---|---|---|---:|
| AAPL | 2019-08-19 | 23:59 | 1,682 |
| AMZN | 2019-08-19 | 23:59 | 1,682 |
| COST | 2019-08-19 | 23:59 | 1,702 |
| CROX | 2018-05-01 | 23:45 | 2,092 |
| GOOG | 2019-08-19 | 23:59 | 1,682 |
| IONQ | 2021-10-01 | 23:56 | 1,209 |
| MU | 2019-08-19 | 23:59 | 1,702 |
| NVDA | 2019-08-19 | 23:57 | 1,674 |
| PATH | 2021-04-21 | 23:17 | 1,323 |
| SNDK | 2025-02-24 | 23:59 | 379 |
| TWST | 2018-10-31 | 23:45 | 1,964 |

| Horizon | Saved cohort rows | Training date range | Boundary exclusions | Further quality exclusions |
|---|---:|---|---:|---:|
| 1h | 171,641 | 2018-05-31–2026-09-24 | 67,262 | 217 |
| 4h | 42,894 | 2018-05-31–2026-09-24 | 25,349 | 66 |
| 1d | 29,642 | 2019-09-18–2026-09-24 | 55,543 | 105 |
| 1w | 5,839 | 2019-09-18–2026-09-18 | 11,108 | 89 |

Five-minute actual-price boundaries and all chronological/quality exclusions remain enforced; conflicting minute rows are zero. Broader archives do not imply every symbol has 2018 data or meets every model requirement.

**Model outcomes.** All four directional groups are **PROMOTED** under their saved v2 gates: Brier no more than baseline +0.005 and log loss no more than baseline +0.01, plus calibration, variation and sample checks. The bounded [directional review](C:/dev/ducketz/artifacts/analysis/overnight-20260925/verification/preliminary-directional-review.md) verified saved gate arithmetic and frozen support; the [independent estimator audit](C:/dev/ducketz/artifacts/analysis/overnight-20260925/verification/yg-completion.json) reproduced every raw/calibrated assessment score exactly, all route/symbol scores and support counts, fixed development selection and calibration diagnostics.

| Horizon | Brier / baseline | Log loss / baseline | Status |
|---|---|---|---|
| 1h | 0.250755552 / 0.250000010 | 0.694678409 / 0.693147200 | PROMOTED within tolerance |
| 4h | 0.249674883 / 0.250062913 | 0.692497652 / 0.693273017 | PROMOTED; beats both |
| 1d | 0.250584667 / 0.250757893 | 0.694320630 / 0.694663326 | PROMOTED; beats both |
| 1w | 0.253966530 / 0.252409482 | 0.701197402 / 0.698003635 | PROMOTED within tolerance |

Hourly and weekly promotion does not claim baseline outperformance. TWST remains thin: hourly 16:00 has one fitted example; daily has two per route and weekly eight fitted/two assessed. Group promotion does not establish precise symbol/route accuracy.

All four separate sizing groups are **FITTED, with zero qualified scopes**. The [sizing review](C:/dev/ducketz/artifacts/analysis/overnight-20260925/verification/sizing-review.md) verified report/model/manifest hashes, source cohort byte hashes, development selection, convergence and unchanged strict gate arithmetic. Hourly/four-hour fail return MSE; daily fails Brier, log loss and return MSE; weekly also fails downside MSE. Optional-input exclusions of 72,749 / 17,846 / 2,141 / 2,081 leave 92,922 / 25,048 / 3,784 / 3,758 eligible sizing rows. No concrete fitting or selection defect justified an assessment-driven retry. These learned sizing statuses remain separate from the manual Gameplan policy.

**Cash/share scenario and disclosures.** The saved plan reports all **154 hourly price points AVAILABLE**, 209 entry price ranges and 55 non-entry rows. Its fresh GET-only snapshot at 06:18:56 UTC uses **$47,346.43 literal cash**, zero reserved cash and zero working orders; the larger $103,815.80 broker capacity is not treated as spendable literal cash. Its shared ledger models **70 conditional events: 47 buys and 23 sales**, with a $2,367.3215 initial buffer and ending cash **$2,402.98–$3,449.80, base $2,925.41**. The no-fill baseline keeps $47,346.43 and the starting holdings. Independent verification reproduced event-by-event conservation, original-row identity and the complete ledger from the saved snapshot. Evidence: [planning report](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260925T061845.719846Z/report.json), [direction ledger](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260925T061845.719846Z/direction-ledger.json).

Conditional ending shares are AAPL 30, AMZN 42, COST 19, CROX 85, GOOG 30, IONQ 237, MU 10, NVDA 47, PATH 527, SNDK 3 and TWST 81. These cash/share totals assume planned fills and available proceeds, before fees/taxes; they are not broker fills, realized P/L or execution authority.

Three current planning anchors explicitly use `ASSUMED_NO_TRADES`, all effective September 25 00:00 UTC / September 24 17:00 Pacific:

| Symbol | Price | Original observed close UTC | Synthetic gap |
|---|---:|---|---:|
| CROX | $123.51 | September 24 23:46 | 14 minutes |
| PATH | $12.63 | September 24 23:18 | 42 minutes |
| TWST | $184.44 | September 24 23:46 | 14 minutes |

The report records 70 current synthetic zero-volume minutes and 388 historical synthetic closing references for planning. Original observations remain separate. The saved sparse-session policy allows up to 240 minutes within verified same-session acquisition coverage; it does not modify native data, training prices, regular-session prices or actuals' five-minute boundaries. Source coverage, all synthetic derivations and unchanged original observations passed independent reproduction. Evidence: [reference completion](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260925T061845.719846Z/planning-reference-completion.json), [synthetic bars](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260925T061845.719846Z/synthetic-reference-bars.parquet).

**Actuals and unresolved coverage.** The immutable September 24 review reports **167 evaluated forecasts, 83 correct calls (49.70%), 31 mature missing and 66 pending**, with **136 price comparisons, 14 within the saved working ranges and 18 missing**. Missing mature forecasts: CROX 11, IONQ 2, PATH 9, TWST 9. Missing same-clock prices: CROX 7, IONQ 1, PATH 4, TWST 6. These use the saved pre-opening September 24 publication and trade plan; no past estimates were rebuilt. Neutral, future and missing outcomes are excluded from directional accuracy. [Actuals report](C:/DATASTORE/ml/gameplan-actuals-review-runs/20260925T062206.766961Z/report.json); the [dated results](C:/DATASTORE/ml/gameplan-actuals-review-by-date/2026-09-24/Gameplan-results.md) are linked from the successor.

Cumulative native evaluation since September 4 contains **5,040 saved forecast rows: 4,043 evaluated, 832 mature missing and 165 pending**. It retains each publication's own universe and probability target; pre-onboarding absence is not missing data, and OG cost-adjusted/YG raw-direction scores must not be pooled as one probability-quality claim. Coverage reproduction, each historical universe and frozen prior-source comparisons all passed.

**Ownership and controls.** Before planning, guarded native reconciliation resolved four terminal CROX/TWST reservations using GET-only order evidence and a newer coherent matching-account snapshot. Three orders had zero fills; TWST's four previously recorded fills were preserved. No new fills, owned-share changes or assignment releases occurred. Native tests passed **86/86** and independent reconciliation checks **384/384**, including all 360 reservation records and unchanged controls/session/entry claims. The existing manual selection, schedules, trading code, risk limits and adaptation pause were preserved; no trader was started. [Reconciliation evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260925/preflight/native-reconciliation-verification.json), [native tests](C:/dev/ducketz/artifacts/analysis/overnight-20260925/native-reconciliation-tests.txt).

The current saved contracts use above/below 50% direction, exact-50% hold, and signal-driven holdings with no scheduled expiry sales. These match the later implementation approvals retained in the current documentation; stale recurring 54%/46%, expiry-sale and fifteen-minute planning prose was not applied to this publication. No policy or trading-control change was made during supervision.

**Final verification:** the [full audit](C:/dev/ducketz/artifacts/analysis/overnight-20260925/verification/completion-audit.json) passed all 12 sections with no errors; [YG model inference](C:/dev/ducketz/artifacts/analysis/overnight-20260925/verification/yg-completion.json) passed with zero score error; [provider hashing](C:/dev/ducketz/artifacts/analysis/overnight-20260925/verification/provider-completion.json) passed all 33 scopes and 66 files. The [independent output review](C:/dev/ducketz/artifacts/analysis/overnight-20260925/verification/independent-output-review.md) passed 3,785 checks, including all 57 guarded files, the entire unchanged post-reconciliation ownership ledger and the disabled weekday 03:55 Windows launcher. Daily automation remains ACTIVE at 21:05 Pacific with failure-only notifications. Existing joblib/NumPy deprecation warnings were nonfatal; no new production code repair was needed. Supervision closure is recorded below.


**Supervision closed.** The helper exited cleanly after 274 successful claim renewals, maximum gap 34.30 seconds; no helper or pending renewal remained. Own UUID `5c3cea33-8624-4dcc-9fa8-68928af785b1` was **RELEASED at 2026-09-25T06:45:46.665893+00:00**. [Release receipt](C:/dev/ducketz/artifacts/analysis/overnight-20260925/supervision-release-final.json), [monitor evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260925/monitor/monitor-summary.json). Completed source September 24 must not be rerun. Final record time: 2026-09-25T06:46:56.855465+00:00.
