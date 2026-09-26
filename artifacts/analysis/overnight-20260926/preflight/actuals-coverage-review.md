# Saved actuals coverage review

Reviewed at 2026-09-26T06:37:44.464048+00:00. Status: `SAVED_ACTUALS_COVERAGE_VERIFIED`; 17 checks passed; no issues.

The immutable September 25 actuals review contains 264 forecasts: 160 evaluated, 38 mature awaiting qualifying observations and 66 pending maturity. Its 154 hourly price points contain 137 comparisons and 17 missing actual prices. Raw direction was correct for 92 of 160 evaluated forecasts (57.5%); 104 missing/future outcomes are excluded. There are no evaluated neutral rows in this saved publication. This count includes eight evaluated opening-gap research rows and describes market direction, not broker fills or realized profit.

| Symbol | Evaluated | Mature missing | Pending | Raw correct/evaluable | Prices compared | Missing prices |
|---|---:|---:|---:|---:|---:|---:|
| AAPL | 18 | 0 | 6 | 10/18 | 14 | 0 |
| AMZN | 18 | 0 | 6 | 10/18 | 14 | 0 |
| COST | 12 | 6 | 6 | 7/12 | 13 | 1 |
| CROX | 7 | 11 | 6 | 5/7 | 7 | 7 |
| GOOG | 17 | 1 | 6 | 12/17 | 14 | 0 |
| IONQ | 16 | 2 | 6 | 10/16 | 13 | 1 |
| MU | 18 | 0 | 6 | 11/18 | 14 | 0 |
| NVDA | 18 | 0 | 6 | 10/18 | 14 | 0 |
| PATH | 7 | 11 | 6 | 1/7 | 9 | 5 |
| SNDK | 18 | 0 | 6 | 9/18 | 14 | 0 |
| TWST | 11 | 7 | 6 | 7/11 | 11 | 3 |

All 76 endpoint checks belonging to the 38 mature-missing forecasts, plus all 17 missing clock prices, retain `VERIFIED_COMPLETE` coverage. Their required intervals fit saved verified source partitions whose metadata paths are bound into the actuals manifest. Complete interval acquisition does not guarantee a qualifying price observation and does not prove that no trades occurred. Root's full audit independently checks source payloads and endpoint selection; this bounded review did not reload raw prices.

The mature-missing forecast endpoints contain 47 `OUTSIDE_TOLERANCE`, three `NO_OBSERVATION` and 26 `OBSERVED` endpoint results. Every rejected candidate is beyond five minutes; actual returns remain absent. The 17 missing clock prices comprise 14 outside tolerance and three CROX no-observation points (14:00, 15:00 and 16:00 Pacific). The saved candidate and required interval details are retained in the JSON evidence.

| Symbol | Missing price clocks (Pacific) |
|---|---|
| COST | 17:00 |
| CROX | 04:00, 05:00, 06:00, 14:00, 15:00, 16:00, 17:00 |
| IONQ | 15:00 |
| PATH | 04:00, 06:00, 13:00, 16:00, 17:00 |
| TWST | 14:00, 15:00, 17:00 |

Forecast endpoint and clock-price counts differ because entry opens and target-end closes use different boundary observations. GOOG therefore has one missing forecast endpoint while every hourly planning-clock comparison is available. The 66 future rows are six per symbol: `4h@16:00`, `1d@D+2` through `1d@D+5`, and `1w@D+5`. They are pending maturity, not mature data failures.

The receipt binds the current manifest; all 13 checked saved-publication input/output hashes and sizes match. All 65 original forecast columns and all 123 original trade-plan columns match their frozen sources exactly across 264 rows. All 154 original hourly low/mid/high estimates and timestamps are unchanged. The historical publication's own `stock-direction-50-v2` directions remain intact; no current threshold policy was applied retroactively.

Source publications: `C:/DATASTORE/ml/nightly-gameplan-runs/20260925T060654.631426Z` and `C:/DATASTORE/ml/gameplan-trade-plan-runs/20260925T061845.719846Z`. Actuals: `C:/DATASTORE/ml/gameplan-actuals-review-runs/20260926T063044.178275Z`. Detailed evidence: `actuals-coverage-review.json`; reproducible helper: `review_actuals_coverage.py`.

No production changes, provider calls, broker calls, synthetic actuals, new estimates or model fits were made. Missing and future outcomes retain honest unavailable/pending statuses.
