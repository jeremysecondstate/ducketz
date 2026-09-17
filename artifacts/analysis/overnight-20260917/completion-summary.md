# September 17 Gameplan completed and verified

The full native run completed at **2026-09-17 05:51:38 UTC / September 16 22:51:38 Pacific**, before its unchanged September 17 04:00 Pacific deadline. All eight stages completed in the original attempt; no repair, recovery, restart, deadline exception or repeated training was needed. The process exited 0. The final offline verifier also exited 0, with all eleven sections VERIFIED and no errors; its overall result is VERIFIED_WITH_COVERAGE_NOTES.

- [Readable September 17 Gameplan](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260917T054857.515593Z/Gameplan.md)
- [September 16 actuals review](C:/DATASTORE/ml/gameplan-actuals-review-runs/20260917T055121.360000Z/Gameplan-results.md)
- [Full final verification](C:/dev/ducketz/artifacts/analysis/overnight-20260917/final-verification.json)
- [Native completed run and operator notes](C:/DATASTORE/ml/overnight-runs/20260917T040722.433471Z/operator-notes.md)

## Publication and models

Production membership is the eleven-symbol watchlist, verified against the ACTIVE September 13 onboarding batch. No onboarding was repeated. The frozen [publication](C:/DATASTORE/ml/nightly-gameplan-runs/20260917T054622.686474Z/gameplan.json) has exactly **264 forecasts and 264 NO_TRADE_STOCK_ONLY intents**, 24 of each per symbol, using independent-stock-targets-v1 and XNAS.ITCH. All 209 entry instructions are bearish. The native run and all preparation outputs report **zero orders**.

All four directional groups passed the recorded v2 assessment policy. Saved estimator inference reproduced their reported scores and calibrated variation; exact fitted symbol/route, source, chronological partition and observed endpoint checks passed. Four-hour and weekly scores are slightly worse than baseline but within the approved tolerances; qualification does not establish baseline outperformance. CROX has fitted daily/weekly support but zero held-out daily/weekly assessment rows, so pooled qualification is not a symbol-specific accuracy claim.

Weekly improvement is supported by new development evidence: nine legitimate cohort rows were appended, with no changes to existing rows. Development selected Platt calibration this time, preserving probability variation. No assessment-driven retuning or concrete defect justified a repair. [Model review and exact scores](C:/dev/ducketz/artifacts/analysis/overnight-20260917/model-review.md).

All four enrichment horizons fitted. Qualified scopes are **1h 137/143, 4h 0/78, 1d 0/11, 1w 0/41**. Unqualified learned sizing remains explicit in the [training report](C:/DATASTORE/ml/stock-trader-model-runs/20260917T054810.913056Z/training-report.json). It is separate from directional qualification and the user's manual Gameplan sizing policy.

## Planning review

All **154 hourly price points** and all 209 entry prices are available. The 264 augmented rows, exact source identity, snapshot, price path, independent capacities, chronological ledger, hourly summaries and cash/share conservation passed deterministic recomputation.

The read-only snapshot at 05:49:37 UTC recovered one working-orders GET timeout within the native retry budget. It reports zero working orders/reservations and cash-only available funds of **$85,282.76**. Broker available funds of $113,835.31 are retained separately and were not silently treated as spendable cash. Existing holdings are 19 AAPL, 18 GOOG, 6 MU, 30 NVDA and 4 SNDK, held in weekly allocations.

The conditional scenario projects five bearish weekly sales at 04:00, **no buys and no expiry sales**. If all planned sales fill, ending cash is **$115,985.87–$116,109.60** (base $116,047.79), with zero remaining configured-stock shares. These are planning assumptions, not broker fills or a guarantee of execution. The no-fill baseline retains the starting cash and shares; independent horizon ownership and protected reservations reconcile.

The current sparse-session policy explicitly carries two verified same-session closing references: **CROX 76 minutes at $115.58; TWST 44 minutes at $143.00**, totaling 120 labeled zero-volume synthetic minutes. Original observed timestamps, separate effective boundary and complete native coverage are retained and disclosed in the readable plan. The audit independently reconstructed these artifacts. Native data, training labels, actuals and live quotes receive no synthetic observations.

[Output and arithmetic review](C:/dev/ducketz/artifacts/analysis/overnight-20260917/output-review.md).

## Actuals and cumulative evaluation

The September 16 review preserves the original pre-opening refreshed trade plan and all saved estimates. It contains **264 forecasts: 163 evaluated, 35 mature awaiting data, 66 pending maturity**. Raw price-direction accuracy is **73/163 = 44.79%**; it is not realized P/L or the cost-adjusted classification score.

Of 154 hourly prices, **135 were compared (34 inside their saved ranges)** and 19 are unavailable. Eighteen have observations outside the five-minute tolerance; one has no eligible observation. All 19 have VERIFIED_COMPLETE source-request coverage. Missing hourly counts are COST 2, CROX 7, PATH 3 and TWST 7. These are observation gaps, not evidence of incomplete downloads; no prices or outcomes were manufactured. The dated reader and successor link match the immutable review.

The [cumulative evaluation](C:/DATASTORE/ml/gameplan-evaluation-runs/20260917T054738.272062Z/summary.json) covers 16 saved publications against their own universes: **2,928 forecasts; 2,329 evaluated, 421 mature awaiting data, 178 pending**. Evaluation precedes the new September 17 publication as required.

## Provider verification and retained limitations

All eleven symbols completed provider capture and fundamentals/technicals/signals with zero blocking provider or optional capture failures, and all 456 logged Parquet outputs exist. All **33 OPRA scopes** completed September 16 coverage through exclusive September 17; no failures, deferrals, capacity blocks or Live replay. Exact preflights quoted **$0**, with 3,640,230,272 estimated bytes and passing capacity checks. Fresh health selects 66,350 verified partitions; this does not claim every retained archive directory is valid. All eleven XNAS history scopes downloaded with zero failures, zero quoted cost and verified source/receipt evidence. [Provider audit](C:/dev/ducketz/artifacts/analysis/overnight-20260917/provider-completion.md).

The known CME stale partition-source advisory remains rejected. Two new bounded MBP captures hit the 5,000-row cap after five window reductions and correctly remain excluded from qualified complete book coverage. The retained FMP clock-skew diagnostic is unchanged. [CME audit](C:/dev/ducketz/artifacts/analysis/overnight-20260917/cme-advisory-audit.md).

The Option Pricing family still points to unchanged August 20 evidence with insufficient freshness, targets and interval/uncertainty fields. All 99 routes correctly excluded it, while the nine Directional models retained their other admitted features. This is separate from successful production OPRA maintenance and was not repaired by weakening a gate. [Pricing quarantine audit](C:/dev/ducketz/artifacts/analysis/overnight-20260917/option-pricing-quarantine.md).

## Ownership and completion

The operator acquired its own supervision UUID `889267f0-2a67-4ebd-89e7-5adefb5bf262`, inspected logs/health and renewed throughout the run and final audit. Only audit artifacts and operator notes were added; no production code, controls, risk limits, raw data, historical publication or schedule was changed. No trader was started and no order was placed, cancelled or replaced. The user-paused daytime supervision and disabled Windows launcher were preserved. Supervision was released at 05:57:01 UTC in [the release receipt](C:/dev/ducketz/artifacts/analysis/overnight-20260917/supervision-release.json); subsequent wakes should recognize this completed source September 16/action September 17 run and not repeat it.
