# September 22 Gameplan completion

The native eight-stage overnight run `20260922T040810.607319Z` completed at **2026-09-22 05:55:05 UTC / September 21 22:55 Pacific**, before its unchanged September 22 04:00 Pacific deadline. Native exit was zero. Both independent final verifiers exited zero, with all eleven full-audit sections verified and no evidence errors. Overall status is **VERIFIED_WITH_COVERAGE_NOTES**.

- [Readable September 22 Gameplan](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260922T055228.508721Z/Gameplan.md)
- [September 21 actuals](C:/DATASTORE/ml/gameplan-actuals-review-runs/20260922T055447.175940Z/Gameplan-results.md)
- [Full verification](C:/dev/ducketz/artifacts/analysis/overnight-20260922/final-verification.json)
- [YG verification](C:/dev/ducketz/artifacts/analysis/overnight-20260922/yg-final-verification.json)

## Verified preparation

Eleven ACTIVE production symbols retain **264 forecasts, 264 stock-only intents and 264 augmented planning rows**. Publication `20260922T054940.677673Z` is YG/raw-price-direction-v1, bound to September 21 inputs and September 22 action date. All eight stages completed once; no restart, stage replay, deadline extension or onboarding repetition occurred. Overnight orders were **zero**.

All eleven provider scopes completed, with zero blocking or optional capture failures and 456 logged output files present. All **33 OPRA scopes** passed exact $0/capacity preflights, current cursor/partition checks and 66 current-source file hashes. XNAS history completed eleven downloads, zero failures, at $0. The current health inventory contains 66,449 selected verified partitions; this does not claim every retained directory is valid. See [provider review](C:/dev/ducketz/artifacts/analysis/overnight-20260922/provider-completion.md).

The configured raw CME scope lacks ESU6/NQU6 observations. All five continuous roots returned data. The retained Option Pricing source remains excluded on all 99 routes under unchanged coverage/freshness gates; no new defect justified a retry. These limitations are documented separately from complete OPRA acquisition.

## Ownership maintenance

After the prior manual session finished, its CROX hourly SELL31 and IONQ hourly BUY48 reservations remained locally WORKING. Fresh exact broker evidence verified both canceled with zero fills. The previously authorized native terminal-evidence maintenance procedure reconciled only this pair under native session/per-cycle locks, with a saved SQLite backup, fresh matching account/portfolio evidence and zero execution budgets. Result: READY, no pending reservations or blocks. Filled holdings, other reservations, controls and entry records were preserved. One open-order GET timeout recovered through the existing retry policy.

No order was placed, canceled or replaced; no trader was started and no production code, controls or schedules were changed. The original planning snapshot is safe and `planning_override` is null. See [reconciliation receipt](C:/dev/ducketz/artifacts/analysis/overnight-20260922/native-reconciliation.json).

## Model and projection limitations

Hourly, four-hour and daily directional groups passed their saved v2 tolerances: **253 promoted rows**. The **11 weekly rows failed** Brier/log-loss tolerances. Weekly scores are 0.261371535 / 0.717420132 against baseline 0.253543516 / 0.700350235. Review found unchanged prior cohort rows, eight valid newly matured targets, correct development selection and no concrete defect warranting repair or another fit. None of the four directional groups beats both baselines. Preserve assessment status; the selected manual policy consumes saved instructions independently of that status.

All four learned sizing models fitted. Hourly qualified 137/143 scopes; four-hour, daily and weekly qualified none. Learned sizing remains separate from the selected manual policy. See [model review](C:/dev/ducketz/artifacts/analysis/overnight-20260922/model-review.md) and [weekly diagnosis](C:/dev/ducketz/artifacts/analysis/overnight-20260922/weekly-diagnosis.md).

Fresh literal cash is **$21,142.53**, with no working orders or reservations. All **154 hourly planning prices** are available. The saved sparse-session policy explicitly carries CROX's $123 close for 7 minutes, PATH's $13.62 close for 29 minutes, and TWST's $165 close for 208 minutes: 244 zero-volume synthetic bars, with original observations and source coverage retained. Training and actuals retain native five-minute checks.

The conditional scenario contains 58 events (38 buys, 20 bearish sales), with ending cash **$26,720.55–$27,457.11**, base $27,091.30. Cash/share conservation and the unchanged no-fill baseline verified. These are planned-fill scenarios, not broker executions. See [output review](C:/dev/ducketz/artifacts/analysis/overnight-20260922/output-review.md) for ending holdings and each synthetic anchor.

## Historical coverage

Cumulative evaluation covers 4,248 saved forecasts against each original universe: 3,341 evaluated, 654 awaiting observations and 253 immature. This cutoff precedes tonight's new publication.

September 21 actuals retain 264 forecasts: **161 evaluated, 37 missing and 66 pending**, with **93/161 raw directions correct (57.76%)**. Monday had no saved pre-opening trade plan, so all 154 original price estimates remain absent; 133 actual same-clock prices are observed and 21 are missing. Complete source acquisition does not fabricate missing observations. The expired prior overnight attempt and its missing historical planning package remain unchanged.
