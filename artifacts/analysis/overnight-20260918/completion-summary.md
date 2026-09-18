# September 18 Gameplan — complete with model and observation limitations

The September 17 source-session workflow completed all eight stages at **2026-09-18 05:51:04 UTC / September 17 22:51:04 Pacific**, in 1 hour 43 minutes. Original native run `20260918T040804.048018Z`, source identity and September 18 04:00 Pacific deadline were preserved. Native process exit was 0. No repair, restart, resume, repeated onboarding, deadline exception or trading action was needed.

The final offline audit exited 0 at 05:55:01 UTC. All eleven sections passed, with status **VERIFIED_WITH_COVERAGE_NOTES** and no verification errors. [Final evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260918/final-verification.json) verifies run ancestry, receipts/log hashes, immutable publication and original deadline, 264 forecasts, 264 stock-only intents, 264 augmented trade-plan rows, all eleven production symbols, source-bound enrichment, planning references, cumulative evaluations, recomputed actuals and zero overnight orders.

- [Readable September 18 Gameplan](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260918T054811.145288Z/Gameplan.md)
- [September 17 actuals review](C:/DATASTORE/ml/gameplan-actuals-review-runs/20260918T055047.136335Z/Gameplan-results.md)
- [Model review and weekly diagnosis](C:/dev/ducketz/artifacts/analysis/overnight-20260918/model-review.md)
- [Provider completion evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260918/provider-completion.md)
- [Planning and actuals details](C:/dev/ducketz/artifacts/analysis/overnight-20260918/output-review.md)

## Model results

Hourly, four-hour and daily direction models passed their recorded v2 criteria. Only hourly outperformed both baselines; passing the allowed tolerances is not a baseline-outperformance claim. Weekly remains `RESEARCH_NOT_PROMOTED`: Brier **0.273979208** versus baseline **0.256634775** (excess 0.017344433, allowed 0.005); log loss **0.745838370** versus **0.706573715** (excess 0.039264655, allowed 0.01). All four saved assessment/raw/baseline results reproduce exactly. Weekly calibration changed from Platt to identity through valid development selection on eight newly mature rows; no concrete source, fitting, scoring or chronology defect justified a repair or retry. Its failed assessment remains unchanged.

All four independent enrichment models fitted; **zero scopes qualified**. Hourly failed return MSE; four-hour failed Brier, log loss and return MSE; daily additionally failed probability variation; weekly failed Brier, log loss, return MSE and adverse-return MSE. Learned sizing qualification remains separate from the manual/fixed sizing policies. This overnight audit neither starts a trader nor establishes execution or fills.

## Providers and prices

All eleven provider capture/calculation scopes completed without blocking or optional capture failures; 456 logged output paths exist. All **33 OPRA scopes** have fresh exact **$0** preflights and passing capacity checks, valid native current-session partitions and cursors through exclusive September 18. Estimated acquisition size was 4,837,988,448 bytes. No Live replay, blocked/deferred scope or acquisition failure occurred. The new health inventory selects 66,383 verified partitions; it does not establish that every retained directory is valid. All eleven XNAS target-history requests completed at **$0**, with zero failures.

Known optional CME/FMP advisories and the unchanged stale/sparse Option Pricing family remain excluded by their existing gates. [Pricing evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260918/pricing-gate-review.md) verifies the same 51 compact source bindings and 99 excluded routes. No source or quality rule was weakened.

The trade plan has **154/154 hourly prices**. CROX alone uses a current synthetic anchor: the observed September 17 **16:53 Pacific** close of **$123.19** is carried seven minutes to 17:00, with seven explicit zero-volume bars and verified same-session coverage. This is `ASSUMED_NO_TRADES`, not an observed exchange close. Original observations, native data, five-minute training/actuals rules and live quote checks remain unchanged.

The fresh read-only account snapshot records **$115,997.00** available cash, one NVDA share, no working orders and no cash reservations. The conditional scenario contains 30 buys and three bearish sales, with no expiry sales. It ends at **$6,031.49–$6,573.29** cash (base $6,304.06), preserving the $5,799.85 buffer; holdings and every event balance pass reconstruction. These are assumed fills, not orders or broker outcomes. The no-fill baseline is retained.

## Observed outcomes

September 17 actuals contain **164 evaluated, 34 mature awaiting observations and 66 future** forecast windows. Of 154 saved price estimates, **137** were compared and **17** lacked observations within five minutes. All missing price ranges have verified complete native request coverage, so unchanged downloading is not a justified remedy. Raw-price direction correctness is 62/164 (37.80%); 11/137 observed prices fell inside the saved ranges. These measures are separate from cost-adjusted model scores and broker P/L.

Cumulative evaluation covers 3,192 forecasts from each saved publication's own universe: **2,543 evaluated, 470 mature awaiting data and 179 pending maturity**. The new September 18 publication follows that evaluation cutoff.

The Windows stock launcher remains disabled, daytime automation monitors remain paused, and all trading controls and code remain unchanged. Root supervision is released after final evidence and automation memory are saved; see `supervision-release.json` for the release receipt.
