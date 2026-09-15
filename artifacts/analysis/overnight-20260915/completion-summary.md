# September 15 Gameplan: complete and verified

The original native overnight run completed all eight stages at **September 14, 22:49:11 Pacific**, taking 1 hour 41 minutes 29 seconds. The original September 15, 04:00 Pacific deadline was preserved. The pipeline placed **zero orders**. No trader was started.

## Published results

- **11 stocks, 264 forecasts, 264 stock-only intents and 264 augmented trade-plan rows.** Universe: AAPL, AMZN, GOOG, MU, NVDA, SNDK, COST, CROX, PATH, TWST and IONQ.
- **All four directional horizons passed their existing promotion checks. All 209 entry forecasts are BEARISH** under the user-approved 50% direction policy. Four bullish opening-gap context rows are not entries. The planning snapshot held no configured stock shares.
- All four learned enrichment horizons were fitted. Daily and weekly learned sizing failed qualification; this sizing is unused by the current manual/fixed-budget quantity policy. Six hourly and two distinct four-hour fitted scopes also lack sufficient pooled route evidence. See [exact model metrics, scopes and reasons](/C:/dev/ducketz/artifacts/analysis/overnight-20260915/model-readiness.md). The daily directional model passed the approved tolerances while scoring worse than its baseline; promotion is not a claim of outperformance.

## Price projection and actuals limitations

**The cash and holdings projection remains `UNAVAILABLE_PRICE_REFERENCES`.** The saved plan retains all 154 hourly price points: 126 available and 28 unavailable, comprising 14 each for IONQ and PATH. Their current 13- and 28-minute reference gaps fit the existing 240-minute after-hours bound. The native guard blocks synthetic completion because loaded historical data contains three undefined IONQ prices from 2022 and one undefined PATH price from 2021. Both fresh partitions cover the required boundary and contain zero undefined prices. No current acquisition failure or concrete code defect was established; the guard and regression test were preserved. [Price evidence](/C:/dev/ducketz/artifacts/analysis/overnight-20260915/price-availability-audit.md).

CROX's planning reference explicitly carries the observed September 14, 15:54 Pacific close of 113.03 to 17:00 using 66 zero-volume synthetic minutes under the existing same-session policy. Its actual timestamp and synthetic assumption remain disclosed; native observations and five-minute label rules are unchanged.

September 14 actuals contain **165 evaluated forecasts, 33 mature forecasts awaiting data and 66 pending forecasts**, from that session's original 11-symbol publication. Of 154 price comparisons, 133 have no saved pre-open estimate and 21 await mature data. The original session had no matching trade plan published before 04:00, so later estimates were not retrofitted. Cumulative evaluation at its saved cutoff covers 14 publications and 2,400 forecasts: 1,894 evaluated, 321 mature awaiting data and 185 pending. Each publication was checked against its own saved universe.

## Source and verification evidence

- Loop A's required provider/features checks completed without blocking capture/calculation failures. Incomplete OPRA Historical metadata correctly triggered the allowed native Live replay: **33 of 33 exact symbol/schema scopes**, 1,236,185,072 bytes, no failed/blocked/deferred scopes. Receipts, acknowledgements, completion, cursor and scope bindings passed. [OPRA audit](/C:/dev/ducketz/artifacts/analysis/overnight-20260915/opra-replay-audit.md).
- The health rebuild reported 66,284 verified selected partitions. It does not expose a total list/count of skipped invalid retained partitions, so this is not a claim that the entire historical archive was validated. All 33 required current scopes were independently verified.
- XNAS.ITCH history published for all 11 stocks, with zero estimated acquisition cost and capacity checks verified. Loop B trained nine models using the unchanged reduced feature contracts where optional Pricing coverage remains below existing thresholds. The stale optional Pricing family and known FMP historical clock-skew/CME old-partition context advisories remain documented. [Pricing coverage](/C:/dev/ducketz/artifacts/analysis/overnight-20260915/option-pricing-quarantine.md); [provider advisories](/C:/dev/ducketz/artifacts/analysis/overnight-20260915/loop-a-provider-advisory-audit.md).
- **All 11 final audit checks passed**, with the coverage notes above. Verification checked native completion, artifact/receipt ancestry and hashes, current sources, model reports, reconstructed price paths/reference completion, shared ledger, cumulative evaluation and actuals. The first audit's sole failure was an artifact helper that assumed Historical completion lines. It was corrected to recognize the exact Historical/Live union. A bounded recheck passed against unchanged overnight ancestry/log hashes. [Final verification with provenance](/C:/dev/ducketz/artifacts/analysis/overnight-20260915/final-verification.json); [unaltered initial audit](/C:/dev/ducketz/artifacts/analysis/overnight-20260915/final-verification-initial.json); [bounded recheck](/C:/dev/ducketz/artifacts/analysis/overnight-20260915/fetch-scope-recheck.json).

## Operations

The task supervised the original run and renewed its own lease while reading new logs and health. No native repair, restart, recovery/resume, repeated onboarding, model retry, production code edit, trading control or schedule change was needed. Latest user-approved direction and sparse-price policies were preserved; [policy provenance](/C:/dev/ducketz/artifacts/analysis/overnight-20260915/policy-provenance.md). The artifact-only verifier correction did not change the pipeline. Final claim release is recorded in `supervision-release.json` alongside this report and in the operator notes.

## Readable publications

- [September 15 Gameplan](/C:/DATASTORE/ml/gameplan-trade-plan-runs/20260915T054710.644968Z/Gameplan.md)
- [September 14 Gameplan results](/C:/DATASTORE/ml/gameplan-actuals-review-runs/20260915T054854.867750Z/Gameplan-results.md)
- [Original overnight stage report](/C:/DATASTORE/ml/overnight-runs/20260915T040742.365640Z/stage-report.json)
- [Operator notes](/C:/DATASTORE/ml/overnight-runs/20260915T040742.365640Z/operator-notes.md)
