# Archive quality exclusions: PASS

Reviewed 2026-09-24T06:44:11.483754+00:00 for C:\DATASTORE\ml\nightly-gameplan-runs\20260924T062615.050242Z.
All 53 warned symbol/schema/date intervals for 2021-07-07, 2021-10-26 and 2022-09-19 are registered. Total exclusions 70: {'PROVIDER_QUALITY_DEGRADED': 53, 'UNDEFINED_OBSERVED_OHLC': 17}.
Retained degraded bars remain in native storage but are excluded from feature contexts. The bad session and next twenty exchange sessions contribute no saved cohort contexts. SNDK warnings predate its first daily observation (2025-02-24), so no daily reset is applicable there.
Every saved cohort has zero target-window overlap with quality exclusions or split/discontinuity boundaries; complete daily contexts are causal and endpoint gaps remain within five minutes.

- 1h: 171,511 rows; 217 native quality exclusions; remaining violating windows/contexts 0.
- 4h: 42,858 rows; 66 native quality exclusions; remaining violating windows/contexts 0.
- 1d: 29,602 rows; 105 native quality exclusions; remaining violating windows/contexts 0.
- 1w: 5,831 rows; 89 native quality exclusions; remaining violating windows/contexts 0.

Verified source manifest bindings, receipt raw checksum identity and raw/normalized sizes; daily normalized payload hashes also checked. Full raw payload audit belongs to parallel verifier and was not repeated.
No code edits, rebuilding, provider access or production changes.

Clock clarification: 5,962 hourly-group rows are explicit non-entry opening-gap research targets starting at prior17:00; their completed daily context is available17:05, before the recorded decision and next04:00 action. Every executable target receives its inputs before target start. This is not a quality-window exclusion failure.
