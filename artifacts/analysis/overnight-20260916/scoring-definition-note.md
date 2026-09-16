# September 15 scoring definitions reconcile exactly

The cumulative evaluation's **113/159 (71.07%)** and actuals review's **90/159 (56.60%)** measure different outcomes. They are not conflicting price observations or evidence of a repair regression.

- [Cumulative evaluation](C:/dev/ducketz/ml/gameplan_evaluation.py:144) calls a row correct when `(predicted_probability >= 0.5)` equals its binary model target. For these forecasts, target 1 means the observed return exceeds the **0.001 (0.10%) assumed round-trip cost**; a flat price or small positive return at/below that hurdle is target 0. Its field is named `direction_accuracy`, but it is cost-adjusted binary-target classification accuracy.
- [Actuals review](C:/dev/ducketz/ml/gameplan_actuals_review.py:198) evaluates the saved direction against the **raw price move**: Bullish requires a strictly positive return; Bearish requires a strictly negative return. A flat move is incorrect for either directional call. Neutral, missing and pending rows are excluded. The separate cost-adjusted target and Brier score remain in the output.

A bounded row-by-row join of [saved evaluation rows](C:/DATASTORE/ml/gameplan-evaluation-runs/20260916T060303.962053Z/evaluations.parquet) and [actuals rows](C:/DATASTORE/ml/gameplan-actuals-review-runs/20260916T061453.637702Z/forecast-results.parquet), restricted to source `20260915T054345.154531Z`, confirms:

- All 264 forecast identities join one-to-one; both datasets score the same 159 mature observations.
- Every observed return matches exactly (maximum difference 0), and every cost-adjusted target and Brier score agrees.
- Correctness differs on exactly 25 rows. **24 bearish forecasts** have nonnegative returns at/below 0.10%: 22 small increases and two flat moves (GOOG 14:00 hourly and SNDK 05:00 hourly). They are correct for the model's target-0 classification, but incorrect raw bearish calls.
- **One bullish MU opening-gap context row** has a small positive return of about 0.0021%, below the cost hurdle. It is correct for raw direction, but incorrect for target-1 classification.
- Thus **113 − 24 + 1 = 90**. The gap-context forecast is an explicitly non-entry research row; both metrics include scored forecasts, not only executed trades.

Use **56.60%** when describing the prior session's raw market-direction accuracy. Label **71.07%** as cost-adjusted target classification accuracy when citing the cumulative field. Neither number is broker-fill accuracy, net trading performance or realized P/L. No production change, tests, training or archive reload was needed for this definition check.
