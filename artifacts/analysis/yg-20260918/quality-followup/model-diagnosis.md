# YG train/development model diagnosis

Created 2026-09-18T07:51:09.812400+00:00. `C:\DATASTORE\ml\nightly-gameplan-runs\20260918T072555.813034Z`.

Train/selection/calibration only. No assessment feature values, labels, predictions, or scores admitted; no fit, fetch, broker, control or production changes.

| Group / partition | Rows | Feature-time clusters | Action sessions | Positive rate | Source snapshots at 17:00 PT | Missing close-location |
|---|---:|---:|---:|---:|---:|---:|
| 1h / train | 53082 | 2230 | 776 | 49.41% | 21.87% | 22.54% |
| 1h / selection | 3392 | 62 | 26 | 48.53% | 81.82% | 52.36% |
| 1h / calibration | 3945 | 63 | 31 | 49.56% | 87.32% | 49.84% |
| 1w / train | 1561 | 629 | 350 | 59.00% | 57.91% | 21.78% |
| 1w / selection | 174 | 54 | 22 | 49.43% | 75.29% | 43.10% |
| 1w / calibration | 198 | 56 | 26 | 43.94% | 78.28% | 42.42% |

## Source and feature findings

All other admitted numeric features are complete in the audited partitions. Missing bar close-location exactly equals zero intrabar range in every audited partition; the source formula deliberately leaves zero-range division undefined. Native median imputation preserves an explicit missingness indicator. This is observable market-feature behavior, not a broken fetch or invented price.

Target entry-clock, weekday, elapsed, trading and closed-market hours are already included. Source bar end-time and age relative to the forecast action are not explicit estimator inputs. Both proposed source timing quantities use already-frozen source timestamps and can be computed identically at training and inference without prices, labels, or a new fetch.

`source__hours_after_regular_close = (source_bar_end_timestamp - source_regular_close) / 1 hour`

`source__hours_until_action_start = (source_action_start - source_bar_end_timestamp) / 1 hour`

The saved-estimator hourly calibration covariance is positive for 14:00/15:00 source bars and negative for 16:00/17:00 bars; the early strata have only 13/11 unique symbol-session snapshots. Weekly 17:00 source bars have a 40% positive rate while predicted raw probability averages 57.0%, but earlier strata are very small. These are development diagnostics, not a demonstrated new-feature model gain.

Exact train redundancies include range-position/range-score and the three direction/upside/downside-pressure variables. Weekly trend and momentum score pairs correlate above 0.97; elapsed duration and closed-market hours correlate 0.9974. Features are causal, but the data do not justify interpreting all 32 numeric weekly inputs as independent information.

## Recommendation

- No corrupted labels, causal price-boundary defect, lost categorical route, or unhandled missing-feature defect identified in the inspected train/development scope.
- Weekly has only 1561 train rows across 350 action sessions, 174 selection rows across 22 sessions and 198 calibration rows across 26 sessions. Six TWST fit rows and no TWST selection/calibration rows limit symbol-specific evidence. Overlapping weekly returns and multiple symbols reduce effective independence further; feature-time cluster counts are not session counts.
- Strong exact and near-exact train feature redundancy plus the small weekly cohort justify the same fixed lower-C logistic grid already used in other horizons. The observed train-to-development base-rate change supports conservative complexity; it does not authorize threshold weakening or selecting by final assessment.
- Flat calibration is an explicit development candidate eligibility mismatch with a downstream varying-probability requirement. Aligning candidate eligibility with that existing requirement before development score comparison is a fixed design repair; preserve the held-out gate and fail honestly if no eligible varying candidate exists.
- Source-bar timing is omitted explicit causal context. Training and development use a different mix of regular-close versus 17:00 features, with zero-range feature missingness following that shift. The proposed two fixed timing features are fully derivable from saved data; no acquisition is needed.
- Calibration-only source-clock associations are mixed and thin in early-hour strata; they do not establish that adding source timing improves unseen results. Do not add a late assessment-driven feature search to the present minimal repair. A prospective development experiment may be justified later.
- A promise that every model will pass cannot be justified from these data. Keep the source/price/sample/promotion gates intact and preserve a failing result if the predeclared development-selected repair still fails.

Evidence and all per-symbol/route/clock counts are retained in `model-diagnosis.json`. This script performed prediction only on saved calibration development rows; it never fitted a candidate or evaluated final assessment.

## Completed fixed source-timing experiment

The separately preregistered train/selection-only comparison added exactly the two timing fields to unchanged HGB/MLP/blends and the fixed logistic C grid. Its best candidate was logistic C=0.001, selection log loss 0.709806570467 versus the original 50/50 blend 0.703697924502: worse by 0.006108645965. No warnings, no calibration/assessment access, and no production changes occurred. Do not integrate this feature family or combine it post hoc with another study. Full distributions and saved selection predictions are in `weekly-source-timing-result.json` and its accompanying Parquet.
