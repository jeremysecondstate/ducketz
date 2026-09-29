# September 29 final independent audit

Status: **VERIFIED_WITH_COVERAGE_NOTES**, recorded 2026-09-29T06:54:38.642963+00:00. Native run: `C:\DATASTORE\ml\overnight-runs\20260929T040727.779758Z`.

The full 12-section audit passed. A later guard stopped on an isolated concurrent Hyperliquid policy edit; original failure and baseline were preserved. Exact-hash and independent 199-module reachability reviews justified continuing only the remaining five checks, which all passed. No native stage repeated.

Archive: 988 manifest-bound files, 950,452,425 bytes; 223 verified second/minute partitions; 16,443,370 seconds and 2,842,334 exact overlapping OHLCV minutes. Seconds added zero examples. All 264 current probabilities and every eligible cohort row reproduced exactly; zero conflicting minute rows admitted.

| Horizon | Cohort rows | First action | Last action | Boundary exclusions | Quality exclusions |
|---|---:|---|---|---:|---:|
| 1h | 171,897 | 2018-05-31 | 2026-09-28 | 67,314 | 217 |
| 4h | 42,965 | 2018-05-31 | 2026-09-28 | 25,366 | 66 |
| 1d | 29,712 | 2019-09-18 | 2026-09-28 | 55,583 | 105 |
| 1w | 5,853 | 2019-09-18 | 2026-09-22 | 11,116 | 89 |

| Symbol | First feature source | Last feature source | Feature rows |
|---|---|---|---:|
| AAPL | 2019-09-17 | 2026-09-28 | 1,684 |
| AMZN | 2019-09-17 | 2026-09-28 | 1,684 |
| COST | 2019-09-17 | 2026-09-28 | 1,704 |
| CROX | 2018-05-30 | 2026-09-28 | 2,094 |
| GOOG | 2019-09-17 | 2026-09-28 | 1,684 |
| IONQ | 2021-10-29 | 2026-09-28 | 1,211 |
| MU | 2019-09-17 | 2026-09-28 | 1,704 |
| NVDA | 2019-09-17 | 2026-09-28 | 1,676 |
| PATH | 2021-05-19 | 2026-09-28 | 1,325 |
| SNDK | 2025-03-24 | 2026-09-28 | 381 |
| TWST | 2018-11-29 | 2026-09-28 | 1,966 |

All four directional groups PROMOTED under saved v2 Brier+0.005/log-loss+0.01 limits. Only 4h/1d beat both baselines. Exact TWST fitted support remains 1 hourly / 24 four-hour / 2 daily / 8 weekly. All four sizing groups FITTED; zero qualified scopes. Assessment failures remain honest; saved evidence shows no actionable fitting defect.

Provider verification passed 33 OPRA scopes/cursors and 66 current data-file hashes. All six CME requests succeeded, all configured symbols observed; both MBP captures capped at 5,000 rows. Exact advisory comparison finds only increasing age for the same stale September 3 NQ derived candidate; exclusion retained. Current FMP quote passes; five September 2 clock-skew rows remain rejected. Separate advisory-resolution evidence closes the review flag without modifying provider evidence.

All 99 optional Pricing gates remain quarantined; 51 compact files / 10,208,882 bytes retain the same authority. Maximum constraints complete/fresh fraction 1.42045%, edge/fair/liquidity 0.602410%, interval/uncertainty 0%; maxima 1 distinct target versus required 20. Monday Loop B correctly has 99 intelligence routes, 88 applicable LIVE forecasts and 11 d5 NOT_APPLICABLE rows. Independent Gameplan remains 264 forecasts / 264 intents.

Planning has 264 rows, 14 clock summaries and 46 events (24 buys / 22 sells), exact cash/share conservation. Starting cash $18.14; conditional ending cash $1.51–$440.41 / base $219.90. Current synthetic planning anchors: CROX 166 minutes, IONQ 10, PATH 150, TWST 69; 395 synthetic one-minute rows, original observations unchanged.

Prior-session actuals: 165 evaluated / 33 mature missing / 66 pending; 84/165 correct (50.91%). Same-clock prices 137 compared / 17 missing. Cumulative 26 publications preserve their own universes: 5,568 forecasts / 4,454 evaluated / 949 mature missing / 165 pending. No synthetic actuals or invented outcomes.

Limits:

- Native raw and normalized files and DBN request headers verified; raw second-level DBN records were not independently replayed.
- Saved original production-cursor content hashes and manifest semantics verified, but native receipt has no separate outer checksum of that snapshot file.
- Four directional groups pass saved v2 tolerances; only 4h and 1d beat both training-rate baselines. Thin exact TWST route evidence is explicit.
- All four sizing groups fitted, zero qualified scopes; no model status was upgraded and no fit/retry performed.
- CME derived stale context and capped MBP, retained FMP clock-skew rows, and 99 optional Pricing route failures remain excluded/limited.
- Planning synthetic references follow current saved contract only. Actual/training price boundaries stay five minutes; no synthetic actuals.
- Concurrent Hyperliquid paper-policy change admitted only at exact reviewed hash; original guard failure/baseline remain preserved.
- Broker controls, ledgers, schedules, claims and bounded planning/actuals outputs independently audited by preflight.

Evidence: [audit-findings.json](C:/dev/ducketz/artifacts/analysis/overnight-20260929/verification/audit-findings.json), [audit-runner-continuation.json](C:/dev/ducketz/artifacts/analysis/overnight-20260929/verification/audit-runner-continuation.json), [completion-audit.json](C:/dev/ducketz/artifacts/analysis/overnight-20260929/verification/completion-audit.json), [provider-advisory-resolution.json](C:/dev/ducketz/artifacts/analysis/overnight-20260929/verification/provider-advisory-resolution.json).
