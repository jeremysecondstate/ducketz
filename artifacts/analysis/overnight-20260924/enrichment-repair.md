# Archive sizing admission repair

The September 24 preparation published its immutable archive-history Gameplan,
then stopped because the sizing fit required eight operational market inputs on
every execution row. Older archive rows correctly contained no such optional
observations. The failure was an integration mismatch, not a nonfinite observed
price or a failed model assessment.

The repair validates all target identities, prices, timestamps and source contracts
before admitting sizing samples. Under the saved archive selector only, rows with
all eight inputs genuinely absent are excluded explicitly. Partial absence,
malformed observations, infinity, missing columns and legacy missing features
remain errors. Nothing is filled or copied from another source. The directional
cohorts and 264 published forecasts remain unchanged.

| Horizon | Sizing samples admitted | Missing optional inputs excluded |
| --- | ---: | ---: |
| 1h | 92,800 | 72,749 |
| 4h | 25,012 | 17,846 |
| 1d | 3,776 | 2,141 |
| 1w | 3,750 | 2,081 |

All eleven symbols retain finite sizing samples. New models and reports bind
counts, per-symbol/per-feature exclusions and an excluded target identity hash.
Native source validation reproduces the admission from immutable cohort files.
Older models retain their saved policy. Fitting features, development selection,
chronological partitions, calibration, qualification gates and trading controls
are unchanged.

Validation: 63 focused tests passed before resume; the final 64-test suite passed
in 25.06 seconds after nullable-selector hardening. Cases cover finite-row
retention, strict legacy loading, invalid target evidence on excluded rows,
partial/malformed/infinite input rejection, unavailable-model reporting and
tampered source-admission evidence. Independent diff review found no blocker.
An unknown nullable archive identity fails closed instead of disappearing through
three-valued indexing; all current cohort identities are valid and unaffected.
Offline admission of the real four immutable cohorts reproduced the table above.

The failed native attempt `20260924T044352.904210Z` was recovered with its terminal
receipt and logs verified. Descendant `20260924T064422.315446Z` resumed only
enrichment, planning and actuals at 06:44 UTC, retaining publication
`20260924T062615.050242Z`, archive/XNAS/raw-direction identity and the original
September 24 11:00 UTC deadline. No completed upstream stage was rerun.

Evidence: `enrichment-repair-tests.txt`, `enrichment-admission-verified.json`,
`enrichment-missing-feature-profile.json`, `enrichment-recovery.txt` and native
`operator-notes.md`. This document records the repair; final receipt verification
is recorded separately after the resumed tail finishes.
