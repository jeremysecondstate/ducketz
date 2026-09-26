# September 28 Gameplan model review

Reviewed 2026-09-26T06:44:48.238127+00:00; immutable publication `C:\DATASTORE\ml\nightly-gameplan-runs\20260926T061604.318956Z`.

Saved directional model qualification is separate from learned sizing qualification. This review never fits or selects candidates using assessment outcomes.

| Group | Directional status | Brier / baseline | Log loss / baseline | Assessment rows / clusters | Smallest exact fitted route |
| --- | --- | --- | --- | --- | --- |
| 1h | PROMOTED | 0.250356280 / 0.250000080 | 0.693873413 / 0.693147340 | 8069 / 63 | TWST 1h@16:00: 1 |
| 4h | PROMOTED | 0.249714674 / 0.250034616 | 0.692576762 / 0.693216420 | 2218 / 63 | TWST 4h@16:00: 24 |
| 1d | PROMOTED | 0.250458645 / 0.250767641 | 0.694069015 / 0.694682815 | 2290 / 63 | TWST 1d@D+1: 2 |
| 1w | PROMOTED | 0.253310454 / 0.251549081 | 0.699878679 / 0.696275053 | 473 / 63 | TWST 1w@D+5: 8 |

Exact fitted symbol/route counts, saved score/gate arithmetic, development-only selection, observed price boundaries and chronological partitions are recorded in the JSON. Each group includes a factual comparison with the frozen September 25 publication; score changes do not select candidates. Qualification within the saved v2 tolerances does not establish baseline outperformance.

| Group | Brier excess / allowed | Log-loss excess / allowed | Strictly beats both baselines | Failed promotion checks |
| --- | ---: | ---: | --- | --- |
| 1h | 0.000356201 / 0.005 | 0.000726073 / 0.010 | False | None |
| 4h | -0.000319942 / 0.005 | -0.000639658 / 0.010 | True | None |
| 1d | -0.000308996 / 0.005 | -0.000613800 / 0.010 | True | None |
| 1w | 0.001761373 / 0.005 | 0.003603626 / 0.010 | False | None |

The JSON records each saved-policy maximum and margin, separate from strict baseline wins. These are factual assessment results and never a candidate-selection rule.

| Group | Learned model fit | Fitted scopes | Qualified scopes |
| --- | --- | ---: | ---: |
| 1h | FITTED | 143 | 0 |
| 4h | FITTED | 78 | 0 |
| 1d | FITTED | 11 | 0 |
| 1w | FITTED | 39 | 0 |

Enrichment source: `C:\DATASTORE\ml\stock-trader-model-runs\20260926T062539.552484Z`. Source cohort binding and native model validity verified; research-only scopes retain their recorded reasons and all execution gates.

Review status: VERIFIED_SAVED_MODELS_WITH_COVERAGE_NOTES. Evidence errors: 0. Full overnight completion is outside this model review.
