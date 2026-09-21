# September 21 YG model review

Reviewed 2026-09-19T06:23:44.103436+00:00; publication `C:\DATASTORE\ml\nightly-gameplan-runs\20260919T061628.240412Z`.

All four freshly fitted directional models and all 264 forecast rows are PROMOTED under the unchanged v2 operating rule. None of the four models beats its training-rate baseline on either Brier score or log loss; each is within the allowed +0.005/+0.01 tolerances. This qualification does not establish baseline outperformance.

| Group | Selected development family | Brier / baseline | Log loss / baseline | Assessment rows / clusters |
| --- | --- | --- | --- | --- |
| 1h | regularized-logistic-c0.1 | 0.250349370 / 0.249988658 | 0.693852038 / 0.693124497 | 4465 / 63 |
| 4h | hist-gradient-mlp-0.25 | 0.250827459 / 0.249991778 | 0.694803663 / 0.693130737 | 1226 / 63 |
| 1d | regularized-logistic-c0.001 | 0.252494358 / 0.250658615 | 0.698215822 / 0.694464709 | 1220 / 63 |
| 1w | hist-gradient-prior-shrinkage-w0.5 | 0.259157343 / 0.255132935 | 0.712226998 / 0.703558601 | 248 / 63 |

Weekly is nearest the allowed limits: Brier excess +0.004024408 leaves 0.000975592 of tolerance; log-loss excess +0.008668397 leaves 0.001331603. Its fixed development search selected the histogram-gradient model with 0.5 shrinkage toward the fitting prior; this is one of the predeclared 36 candidates. The selected family has the lowest saved development log loss. No candidate was selected using this review or final assessment.

Daily calibration retains varying probabilities, but its entire assessment range is 0.406903–0.443867. The four-hour range is also narrow at 0.471590–0.487324. These are explicit varying maps, not constant fallbacks; narrow or one-sided ranges are limitations, not evidence of a violated current gate.

## Fitted-history and assessment limits

All 264 exact symbol/route fitted counts and statuses agree with a new rollup of the saved TRAIN plus selection partitions. There are no retained champions. All saved cohort target labels match raw return > 0 and preserve separate cost-adjusted labels. All twelve adjacent partition boundaries put the prior label end before the next target start and before the next 17:05 post-close source cutoff.

| Group | Smallest fitted exact route | Fitted rows | Companies with no assessment rows |
| --- | --- | ---: | --- |
| 1h | TWST / 1h@15:00 | 2 | None |
| 4h | TWST / 4h@16:00 | 18 | None |
| 1d | TWST / 1d@D+1 | 1 | CROX |
| 1w | TWST / 1w@D+5 | 6 | CROX |

TWST has only five total fitted daily rows, one per daily route; its ten daily assessment rows provide two per route. It has six fitted weekly rows and two weekly assessment rows. CROX has 120 fitted daily rows (24 per route) and 30 fitted weekly rows, but no daily or weekly assessment rows. Aggregate group promotion does not establish predictive accuracy for either company or every route. The current exact-history gate requires positive fitted support, not a per-company assessment guarantee.

The four group assessments each contain 63 distinct decision clusters; their row counts are correlated across symbols and daily outlooks, so they are not that many independent trials. This review does not claim an untouched future test, causal independence of all rows, or tradable profitability.

## Verification and next step

Native immutable publication/target verification, cohort labels, duplicate checks, observed five-minute boundaries, partition counts, source-cutoff chronology, saved-metric promotion arithmetic and exact fitted support all passed. No concrete defect requiring a repair or retry was found. No model was fitted, relabeled or selected again; no account or market archive was read. The subsequent publication-only supplement reproduced all four saved assessment/raw/baseline scores exactly (maximum error 0.0), with all model artifact checks passing. Its explicit status is MODEL_ARTIFACTS_VERIFIED_NATIVE_FAILED and full_native_completion_verified is false; no native completion is claimed. See yg-publication-verification.json.

Structured evidence: `model-review.json` in this directory. Optional enrichment qualification is separate. The native tail subsequently failed its account-ownership snapshot gate; this model review does not establish full overnight completion or authority to retry that gate.
