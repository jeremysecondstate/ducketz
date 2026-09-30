# September 30 Gameplan model review

Reviewed 2026-09-30T06:48:38.731461+00:00; immutable publication `C:\DATASTORE\ml\nightly-gameplan-runs\20260930T061501.586401Z`.

Saved directional model qualification is separate from learned sizing qualification. This review never fits or selects candidates using assessment outcomes.

| Group | Directional status | Brier / baseline | Log loss / baseline | Assessment rows / clusters | Smallest exact fitted route |
| --- | --- | --- | --- | --- | --- |
| 1h | PROMOTED | 0.250275974 / 0.249999738 | 0.693711590 / 0.693146656 | 8071 / 63 | TWST 1h@16:00: 1 |
| 4h | PROMOTED | 0.249928310 / 0.250067329 | 0.693004755 / 0.693281847 | 2221 / 63 | TWST 4h@16:00: 24 |
| 1d | PROMOTED | 0.251006402 / 0.250798327 | 0.695167195 / 0.694744139 | 2295 / 63 | TWST 1d@D+1: 2 |
| 1w | PROMOTED | 0.252118575 / 0.251934172 | 0.697415401 / 0.697044808 | 472 / 63 | TWST 1w@D+5: 8 |

Exact fitted symbol/route counts, saved score/gate arithmetic, development-only selection, observed price boundaries and chronological partitions are recorded in the JSON. Each group includes a factual comparison with the frozen September 29 publication; score changes do not select candidates. Qualification within the saved v2 tolerances does not establish baseline outperformance.

| Group | Brier excess / allowed | Log-loss excess / allowed | Strictly beats both baselines | Failed promotion checks |
| --- | ---: | ---: | --- | --- |
| 1h | 0.000276236 / 0.005 | 0.000564934 / 0.010 | False | None |
| 4h | -0.000139018 / 0.005 | -0.000277093 / 0.010 | True | None |
| 1d | 0.000208075 / 0.005 | 0.000423056 / 0.010 | False | None |
| 1w | 0.000184403 / 0.005 | 0.000370594 / 0.010 | False | None |

The JSON records each saved-policy maximum and margin, separate from strict baseline wins. These are factual assessment results and never a candidate-selection rule.

| Group | Learned model fit | Fitted scopes | Qualified scopes |
| --- | --- | ---: | ---: |
| 1h | FITTED | 143 | 0 |
| 4h | FITTED | 78 | 0 |
| 1d | FITTED | 11 | 0 |
| 1w | FITTED | 39 | 0 |

Enrichment source: `C:\DATASTORE\ml\stock-trader-model-runs\20260930T062446.604631Z`. Source cohort binding and native model validity verified; research-only scopes retain their recorded reasons and all execution gates.

Review status: VERIFIED_SAVED_MODELS_WITH_COVERAGE_NOTES. Evidence errors: 0. Full overnight completion is outside this model review.
