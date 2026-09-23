# September 22 Gameplan model review

Reviewed 2026-09-22T05:55:40.210773+00:00; immutable publication `C:\DATASTORE\ml\nightly-gameplan-runs\20260922T054940.677673Z`.

Saved directional model qualification is separate from learned sizing qualification. This review never fits or selects candidates using assessment outcomes.

| Group | Directional status | Brier / baseline | Log loss / baseline | Assessment rows / clusters | Smallest exact fitted route |
| --- | --- | --- | --- | --- | --- |
| 1h | PROMOTED | 0.250172762 / 0.250052319 | 0.693497168 / 0.693251827 | 4484 / 63 | TWST 1h@15:00: 2 |
| 4h | PROMOTED | 0.250835249 / 0.249987485 | 0.694818889 / 0.693122150 | 1230 / 63 | TWST 4h@16:00: 18 |
| 1d | PROMOTED | 0.253619695 / 0.250402207 | 0.700471030 / 0.693951795 | 1226 / 63 | TWST 1d@D+1: 1 |
| 1w | RESEARCH_NOT_PROMOTED | 0.261371535 / 0.253543516 | 0.717420132 / 0.700350235 | 255 / 63 | TWST 1w@D+5: 6 |

Exact fitted symbol/route counts, saved score/gate arithmetic, development-only selection, observed price boundaries and chronological partitions are recorded in the JSON. Each group includes a factual comparison with the frozen September 21 publication; score changes do not select candidates. Qualification within the saved v2 tolerances does not establish baseline outperformance.

| Group | Learned model fit | Fitted scopes | Qualified scopes |
| --- | --- | ---: | ---: |
| 1h | FITTED | 143 | 137 |
| 4h | FITTED | 78 | 0 |
| 1d | FITTED | 11 | 0 |
| 1w | FITTED | 41 | 0 |

Enrichment source: `C:\DATASTORE\ml\stock-trader-model-runs\20260922T055140.159566Z`. Source cohort binding and native model validity verified; research-only scopes retain their recorded reasons and all execution gates.

Review status: VERIFIED_SAVED_MODELS_WITH_COVERAGE_NOTES. Evidence errors: 0. Full overnight completion is outside this model review.

The six hourly enrichment scopes without qualification are CROX 16:00 and TWST 04:00, 05:00, 14:00, 15:00 and 16:00; each lacks sufficient pooled fitted evidence. Daily and weekly directional assessment contain no CROX outcomes, and TWST daily fitted support is only one row per route, so aggregate directional promotion does not establish every symbol/route accuracy.

The weekly model diagnosis in `weekly-diagnosis.md` finds no concrete repair justified: all 2,274 prior rows are unchanged; eight matured targets shift fixed partitions, and the newly selected candidate is the correct narrow development winner. Nine weekly fit steps reported zero fitting warnings. Preserve the weekly research status.
