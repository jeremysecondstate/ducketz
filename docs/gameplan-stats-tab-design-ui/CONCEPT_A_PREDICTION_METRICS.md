# Concept A — prediction performance

The current [Concept A image](C:/dev/ducketz/docs/gameplan-stats-tab-design-ui/concept-a-session-scorecard.png) preserves the original layout while replacing planning-price metrics with saved-model prediction metrics. This is a static design update, not application implementation.

## What changed

| Previous display | Current display |
|---|---|
| Price-range hits | Probability error (Brier score): **0.222** |
| Mean absolute planning-price error | Bullish-call accuracy: **25.0%, 1/4** |
| Hourly price coverage | Bearish-call accuracy: **55.4%, 46/83** |
| Company planning-price columns | Per-company Brier score and the count of scored bullish/bearish calls |
| Selected company's in-range count | Selected company's Brier score and evaluated forecast count |
| Price-range map | Actual correctness of each saved one-hour directional prediction |

Overall direction accuracy remains **54.0%, 47/87**. Outcome coverage remains **121 evaluated, 42 awaiting maturity, 5 awaiting data**; this is supporting context, not a performance score.

## Metric definitions

**Direction accuracy** compares the frozen bullish/bearish call to the sign of the actual return over its saved target window. Neutral calls receive no directional score. Bullish and bearish accuracy apply the same check separately to each side. The count is always visible: the bullish result rests on only four scored calls.

**Probability error (Brier)** averages the squared difference between the saved calibrated probability and the actual binary target. Lower is better; zero is perfect. **0.222 is a score, not a percentage of incorrect calls.** In this saved review the model's binary target is whether the return exceeds **0.10%** over its target window. This is distinct from the simple positive/negative direction check. The target definition must remain fixed when reviewing the saved predictions.

Direction accuracy uses **87 evaluated nonneutral calls**. Brier uses **all 121 evaluated forecasts**, including **34 neutral** calls: even a neutral directional label retains a saved probability. The summary includes all saved horizons and opening-gap research rows. Pending and missing outcomes are excluded from both performance calculations.

The bottom grid covers only the **13 one-hour execution windows from 04–05 through 16–17 Pacific**, with seven companies: 91 cells. It excludes the separate opening-gap research route and longer horizons. A final **1h score** column gives correct/scored counts within that grid:

| AAPL | AMZN | COST | GOOG | MU | NVDA | SNDK |
|---|---|---|---|---|---|---|
| 6/12 | 8/13 | 4/10 | 8/13 | 5/7 | 9/13 | 1/4 |

Green check: correct. Coral cross: incorrect. Gray dash: neutral. Amber question mark: awaiting actual data. These states were read from the saved forecast results, not inferred from the earlier price-range map.

For an implementation, an information tooltip should carry the Brier target definition, the populations above and the meaning of the Bull / Bear calls count. The count column is a mix of predictions, not a correct/total ratio.

## Verification and artifacts

- Verified the saved review manifest and read [forecast-results.parquet](C:/DATASTORE/ml/gameplan-actuals-review-runs/20260911T061544.303359Z/forecast-results.parquet).
- Recomputed Brier values from frozen probabilities and observed targets, and checked target values against the actual returns. Original result data was not changed.
- [Exact metric and grid snapshot](C:/dev/ducketz/docs/gameplan-stats-tab-design-ui/concept-a-prediction-metrics-september-10.json)
- [Built-in imagegen prompts](C:/dev/ducketz/docs/gameplan-stats-tab-design-ui/CONCEPT_A_PREDICTION_EDIT_PROMPT.md)
- [Original Concept A backup](C:/dev/ducketz/docs/gameplan-stats-tab-design-ui/concept-a-session-scorecard-v1-planning-ranges.png)

Final labels, numbers, 13 unique time windows, 91 grid states and seven grid score counts were visually reviewed. Decorative bar lengths and generated company marks remain illustrative; the numerical labels and discrete grid states are the reference. Concepts B and C remain earlier alternatives and still contain planning-price measures. No application code, forecasts, planning rules, trading controls, schedules or broker state changed.

