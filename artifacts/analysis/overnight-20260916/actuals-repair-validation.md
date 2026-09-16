# Actuals v3 planning-close compatibility repair

At 2026-09-16 06:13 UTC, the relevant actuals, price-band, and price-completion suites passed: 152 tests in 15.58 seconds. `git diff --check` passed. An independent read-only source review found no actionable issues.

The September 15 immutable planning path uses native contract `conditional-hourly-planning-price-path-v3`. Its 17:00 endpoints are named `planning_close`, including observed-only and unavailable estimates. All 154 saved point identities and market clocks match; the actuals reader incorrectly required the legacy `observed_close` name for all versions. Four positive regression cases reproduced this failure before the repair.

`ml/gameplan_actuals_review.py` now requires the v3 endpoint name only for that exact saved contract and only at 17:00. Legacy contracts retain their existing endpoint requirement. Source identity, date, timestamp, and pre-open publication checks remain intact. Actual-price lookup still requires genuine completed bars within five minutes; the saved planning reference completion policy is not applied to observations or targets.

Nine added regression cases cover native sparse planning output against fresh and six-minute-stale actual closes, unavailable references and samples, version and clock mismatch rejection, and preservation of the saved estimates. No raw data, model, trading controls, publication, assessment threshold, or immutable prior artifact was edited.

Recovery decision: verify the exited initial attempt's native FAILED receipt and process identities, then resume only `gameplan_actuals_review`. Retain seven completed stages, the pinned source and Gameplan, and the original 2026-09-16T11:00:00Z deadline. No retraining or deadline exception.
