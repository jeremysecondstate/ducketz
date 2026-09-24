# September 24 Gameplan model review

Reviewed 2026-09-24T06:46:39.752970+00:00; immutable publication `C:\DATASTORE\ml\nightly-gameplan-runs\20260924T062615.050242Z`.

Saved directional model qualification is separate from learned sizing qualification. This review never fits or selects candidates using assessment outcomes.

| Group | Directional status | Brier / baseline | Log loss / baseline | Assessment rows / clusters | Smallest exact fitted route |
| --- | --- | --- | --- | --- | --- |
| 1h | PROMOTED | 0.250205650 / 0.250000030 | 0.693565846 / 0.693147241 | 8070 / 63 | TWST 1h@16:00: 1 |
| 4h | PROMOTED | 0.249775265 / 0.250088670 | 0.692697121 / 0.693324536 | 2217 / 63 | TWST 4h@16:00: 23 |
| 1d | PROMOTED | 0.250437820 / 0.250881895 | 0.694027426 / 0.694911405 | 2290 / 63 | TWST 1d@D+1: 2 |
| 1w | PROMOTED | 0.255523283 / 0.253075237 | 0.704448507 / 0.699339925 | 474 / 63 | TWST 1w@D+5: 8 |

Exact fitted symbol/route counts, saved score/gate arithmetic, development-only selection, observed price boundaries and chronological partitions are recorded in the JSON. Each group includes a factual comparison with the frozen September 23 publication; score changes do not select candidates. Qualification within the saved v2 tolerances does not establish baseline outperformance.

| Group | Brier excess / allowed | Log-loss excess / allowed | Strictly beats both baselines | Failed promotion checks |
| --- | ---: | ---: | --- | --- |
| 1h | 0.000205619 / 0.005 | 0.000418605 / 0.010 | False | None |
| 4h | -0.000313405 / 0.005 | -0.000627416 / 0.010 | True | None |
| 1d | -0.000444075 / 0.005 | -0.000883979 / 0.010 | True | None |
| 1w | 0.002448046 / 0.005 | 0.005108582 / 0.010 | False | None |

The JSON records each saved-policy maximum and margin, separate from strict baseline wins. These are factual assessment results and never a candidate-selection rule.

| Group | Learned model fit | Fitted scopes | Qualified scopes |
| --- | --- | ---: | ---: |
| 1h | FITTED | 143 | 0 |
| 4h | FITTED | 78 | 0 |
| 1d | FITTED | 11 | 0 |
| 1w | FITTED | 39 | 0 |

Enrichment source: `C:\DATASTORE\ml\stock-trader-model-runs\20260924T064431.699829Z`. Source cohort binding and native model validity verified; research-only scopes retain their recorded reasons and all execution gates.

Review status: VERIFIED_SAVED_MODELS_WITH_COVERAGE_NOTES. Evidence errors: 0. Full overnight completion is outside this model review.
