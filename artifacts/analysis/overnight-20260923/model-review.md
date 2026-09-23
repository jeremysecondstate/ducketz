# September 23 Gameplan model review

Reviewed 2026-09-23T05:48:30.063506+00:00; immutable publication `C:\DATASTORE\ml\nightly-gameplan-runs\20260923T054513.490247Z`.

Saved directional model qualification is separate from learned sizing qualification. This review never fits or selects candidates using assessment outcomes.

| Group | Directional status | Brier / baseline | Log loss / baseline | Assessment rows / clusters | Smallest exact fitted route |
| --- | --- | --- | --- | --- | --- |
| 1h | PROMOTED | 0.250159059 / 0.250061573 | 0.693469493 / 0.693270336 | 4598 / 63 | TWST 1h@15:00: 2 |
| 4h | PROMOTED | 0.250876692 / 0.249988641 | 0.694901793 / 0.693124462 | 1262 / 63 | TWST 4h@16:00: 18 |
| 1d | PROMOTED | 0.254018152 / 0.250284369 | 0.701266632 / 0.693716069 | 1261 / 63 | TWST 1d@D+1: 1 |
| 1w | RESEARCH_NOT_PROMOTED | 0.260511989 / 0.252768096 | 0.714423656 / 0.698791351 | 256 / 63 | TWST 1w@D+5: 6 |

Exact fitted symbol/route counts, saved score/gate arithmetic, development-only selection, observed price boundaries and chronological partitions are recorded in the JSON. Each group includes a factual comparison with the frozen September 22 publication; score changes do not select candidates. Qualification within the saved v2 tolerances does not establish baseline outperformance.

| Group | Brier excess / allowed | Log-loss excess / allowed | Strictly beats both baselines | Failed promotion checks |
| --- | ---: | ---: | --- | --- |
| 1h | 0.000097486 / 0.005 | 0.000199157 / 0.010 | False | None |
| 4h | 0.000888052 / 0.005 | 0.001777330 / 0.010 | False | None |
| 1d | 0.003733783 / 0.005 | 0.007550563 / 0.010 | False | None |
| 1w | 0.007743893 / 0.005 | 0.015632306 / 0.010 | False | brier_within_baseline_tolerance, log_loss_within_baseline_tolerance |

The JSON records each saved-policy maximum and margin, separate from strict baseline wins. These are factual assessment results and never a candidate-selection rule.

| Group | Learned model fit | Fitted scopes | Qualified scopes |
| --- | --- | ---: | ---: |
| 1h | FITTED | 143 | 137 |
| 4h | FITTED | 78 | 0 |
| 1d | FITTED | 11 | 0 |
| 1w | FITTED | 41 | 0 |

Enrichment source: `C:\DATASTORE\ml\stock-trader-model-runs\20260923T054714.752473Z`. Source cohort binding and native model validity verified; research-only scopes retain their recorded reasons and all execution gates.

Review status: VERIFIED_SAVED_MODELS_WITH_COVERAGE_NOTES. Evidence errors: 0. Full overnight completion is outside this model review.
