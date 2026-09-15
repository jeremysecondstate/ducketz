# September 15 model readiness

Reviewed 2026-09-15T05:50:42.760905+00:00.

**All four directional groups and all 264 forecast rows are PROMOTED. Every one of the 209 entry rows is BEARISH under the new 50% direction policy.** The four bullish rows are opening-gap research for IONQ, MU, SNDK and TWST; they are not entries. There are no neutral rows. Each of the eleven stocks has nineteen bearish entry windows.

No unexpected blocker was found in this bounded report/table inspection. Native source, manifest and execution checks remain the supervising task’s responsibility.

## Directional assessments

Policy: `independent-stock-directional-promotion-v2`; Brier tolerance +0.005 and log-loss tolerance +0.01 against the saved training-base-rate baseline. Every recorded promotion check passes. The 1d result is worse than its baseline on both metrics, but remains inside these authorized tolerances. Promotion does not demonstrate statistically reliable predictive advantage.

| Horizon | Selected family | Calibration | Assessment rows | Brier / baseline | Log loss / baseline | ECE |
| --- | --- | --- | --- | --- | --- | --- |
| 1h | regularized-logistic-c1 | none | 4215 | 0.228428703 / 0.236631709 | 0.647989279 / 0.666195165 | 0.011110983 |
| 4h | hist-gradient-mlp-0.75 | platt | 1161 | 0.248195794 / 0.248234469 | 0.689535293 / 0.689612302 | 0.015610840 |
| 1d | regularized-logistic-c0.001 | platt | 1149 | 0.251621965 / 0.249614979 | 0.696511204 / 0.692377104 | 0.056907470 |
| 1w | hist-gradient-mlp-0.50 | platt | 233 | 0.254755865 / 0.254796690 | 0.702753305 / 0.702883517 | 0.060049391 |

The selected logistic regularization C is 1 for 1h and 0.001 for 1d. Full precision metrics, deltas, gates, calibration ranges and numerical baselines are retained in [model-readiness.json](/C:/dev/ducketz/artifacts/analysis/overnight-20260915/model-readiness.json).

## Learned enrichment: fitted versus qualified

All horizons are FITTED; qualification uses `pooled-horizon-with-observed-target-coverage-v1`. Fitted scopes include distinct elapsed durations such as holiday/weekend windows, so those totals differ from today’s entry counts. Selection reports explicitly state that assessment outcomes were not used to choose head penalties or calibration.

| Horizon | Fitted scopes | Qualified scopes | Today ready / total | Why remaining scopes are research |
| --- | --- | --- | --- | --- |
| 1h | 143 | 137 | 137 / 143 | INSUFFICIENT_POOLED_SYMBOL_ROUTE_EVIDENCE: 6 |
| 4h | 78 | 76 | 43 / 44 | INSUFFICIENT_POOLED_SYMBOL_ROUTE_EVIDENCE: 2 |
| 1d | 11 | 0 | 0 / 11 | HELD_OUT_HORIZON_QUALITY_NOT_PROMOTED: 10; INSUFFICIENT_POOLED_SYMBOL_ROUTE_EVIDENCE: 1 |
| 1w | 41 | 0 | 0 / 11 | HELD_OUT_HORIZON_QUALITY_NOT_PROMOTED: 39; INSUFFICIENT_POOLED_SYMBOL_ROUTE_EVIDENCE: 2 |

1d horizon quality fails the strict Brier/log-loss baseline checks and probability-variation check. Its assessment probabilities are constant at 0.4203635559504761. 1w fails Brier, log loss, return MSE and adverse MSE comparisons. These learned-sizing results do not relabel the separately promoted directional models.

### Current unqualified learned scopes

All 1d and 1w entries remain unqualified for learned sizing across all eleven symbols. Additional intraday scopes are:

| Symbol | Horizon/entry route | Exact elapsed minutes | Reason | Pooled fit clusters |
| --- | --- | --- | --- | --- |
| CROX | 1h@16:00 | 60m | INSUFFICIENT_POOLED_SYMBOL_ROUTE_EVIDENCE | 9 |
| TWST | 1h@04:00 | 60m | INSUFFICIENT_POOLED_SYMBOL_ROUTE_EVIDENCE | 14 |
| TWST | 1h@05:00 | 60m | INSUFFICIENT_POOLED_SYMBOL_ROUTE_EVIDENCE | 14 |
| TWST | 1h@14:00 | 60m | INSUFFICIENT_POOLED_SYMBOL_ROUTE_EVIDENCE | 3 |
| TWST | 1h@15:00 | 60m | INSUFFICIENT_POOLED_SYMBOL_ROUTE_EVIDENCE | 2 |
| TWST | 1h@16:00 | 60m | INSUFFICIENT_POOLED_SYMBOL_ROUTE_EVIDENCE | 2 |
| TWST | 4h@16:00 | 900m | INSUFFICIENT_POOLED_SYMBOL_ROUTE_EVIDENCE | 18 |

The JSON retains every current unqualified scope, including daily/weekly reasons and observed fit/assessment counts. No fitted horizon reports a symbol entirely without targets.

## Symbol and route evidence limits

No published route is `RESEARCH_NO_TARGET_HISTORY`, and every row has positive exact symbol/route fitted-history count. Some have zero local assessment rows even though their pooled horizon passes. This is an assessment-coverage limitation, not a claim that a route has no fitted history:

| Symbol | Routes with zero local assessment rows |
| --- | --- |
| CROX | 1h@15:00, 1h@16:00, 4h@12:00, 1d@D+1, 1d@D+2, 1d@D+3, 1d@D+4, 1d@D+5, 1w@D+5 |
| TWST | 1h@04:00, 1h@15:00 |

## Evidence and scope

- [Directional model reports](/C:/DATASTORE/ml/nightly-gameplan-runs/20260915T054345.154531Z/model-reports.json)
- [Frozen forecast table](/C:/DATASTORE/ml/nightly-gameplan-runs/20260915T054345.154531Z/forecasts.parquet)
- [Enrichment training report](/C:/DATASTORE/ml/stock-trader-model-runs/20260915T054620.159287Z/training-report.json)

Only the requested Markdown/JSON artifacts were written. No full source rehash, provider/broker call, model retraining, publication change, trading control, order or supervision claim was performed.
