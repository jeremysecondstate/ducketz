# Prediction accuracy investigation

The September 17 result warrants targeted model research. No challenger has been fitted or shown to improve performance, and no production code, publication, controls, schedules or orders were changed.

## Verified saved performance

The native Stats reader verified each dated pointer, receipt and manifest. This covers the seven available daily review snapshots, using each publication's own universe and model approval status. Results exclude missing, immature and neutral directional outcomes. These are descriptive observations; overlapping symbols and target windows do not constitute independent trials.

| Session | Correct / scored | Direction accuracy |
|---|---:|---:|
| September 9 | 44 / 88 | 50.0% |
| September 10 | 47 / 87 | 54.0% |
| September 11 | 49 / 105 | 46.7% |
| September 14 | 66 / 141 | 46.8% |
| September 15 | 90 / 159 | 56.6% |
| September 16 | 73 / 163 | 44.8% |
| September 17 | 62 / 164 | 37.8% |
| Pooled | 431 / 907 | 47.5% |

889 of 907 scored calls (98.0%) were bearish. On those same scored rows, always-up was 51.0% and always-down 48.0%; neither is a deployable recommendation. Seven sessions, varying universes/coverage and correlated outcomes are insufficient for a robust significance or long-term performance claim.

## Target mismatch merits testing first

`ml/independent_stock_targets.py:182-183` defines the training target as observed return exceeding assumed round-trip cost (normally 0.001 = 0.10%). `ml/gameplan_actuals_review.py:195-218` and `app/ui/gameplan_stats_data.py:217-226` separately calculate raw sign correctness and cost-adjusted probability errors. The distinction in the stored metrics is intentional. However, treating a low probability of exceeding costs as a raw bearish probability conflates two different events.

On September 17, all 164 scored forecasts were bearish. Actual moves were 62 negative, 3 flat, 24 positive but at most 0.10%, and 75 positive by more than 0.10%. Thus raw direction accuracy is 62/164 = 37.8%, while classification of the cost-adjusted binary target is 89/164 = 54.3%. The 24 small gains account for part of the discrepancy; 75 larger upward moves remain clear bearish misses. This diagnosis does not establish that retraining on raw direction would have predicted those moves correctly.

Saved Brier 0.2412877260 evaluates the cost-adjusted event. Reinterpreting the same probabilities as raw-up probabilities produces a descriptive Brier of 0.2808220198, versus 0.25 for a constant 50% prediction. This is a semantic diagnostic, not a replacement official score or a reason to relabel existing publications.

The current on-disk direction policy is `stock-direction-50-v2` (>50% bullish, <50% bearish). Saved probabilities and directions were used directly; older automation text describing 54/46 bands was not substituted. No threshold changes were made.

## Proposed bounded experiments

1. Train a separate raw-direction research target, return > 0, retaining the cost-adjusted opportunity target independently. Keep rows, source prices, features, partitions and model families identical for the initial comparison. Preserve the source and five-minute observation gates. Version the different target explicitly rather than silently replacing a frozen contract.
2. Independently test logistic C = 0.001, 0.01, 0.1, 1 for hourly and four-hour research candidates. This is the existing daily grid; other groups currently use C=1 only. In the latest hourly development results, the selected HGB/MLP blend and C=1 logistic are nearly tied (log loss 0.6536701073 and 0.6536733352). This motivates a bounded simpler-model experiment, without demonstrating improvement.

Keep those experiments separate initially so the effect of target alignment and regularization can be identified. Use purged chronological decision clusters, train-only preprocessing, development-only parameter selection and separate calibration. Report target-matched Brier/log loss, raw direction and balanced accuracy, coverage, bullish/bearish breakdowns and per-session/per-horizon results against baselines estimated from training. Uncertainty must account for session correlation.

September 17 and previously inspected assessments are diagnostic evidence, not fresh unbiased tests. Freeze a selected challenger before a prospective observation period and require evidence on unseen sessions before any production replacement. Preserve all original artifacts, promotion policies and trading checks. Do not restart legacy adaptation or a duplicate nightly pipeline for these experiments.

Methodological references: [scikit-learn cross-validation and test-set leakage](https://scikit-learn.org/stable/modules/cross_validation.html) and [probability calibration and proper scoring rules](https://scikit-learn.org/stable/modules/calibration.html).

Reproducible local evidence: `review_recent_sessions.py`, `recent-sessions.json`, plus the prior immutable model audit `../model-review.md`.
