# September 16 saved trade-plan audit

Bounded read-only audit completed **2026-09-16 06:17:12 UTC**. Current trade plan `20260916T060421.076865Z` is `COMPLETE`, bound to Gameplan `20260916T060149.932707Z`, with **264 rows / 24 per symbol**, 264 stock-only option intents, and original/effective deadline **September 16 04:00 Pacific**. Receipt-to-manifest binding passes. Saved receipt, report, snapshot and ledger all report **zero orders**, with broker execution disabled.

The account snapshot was acquired at **September 15 23:05:04 Pacific** (06:05:04 UTC), before publication. It is `CURRENT_AFTER_RETRY`: two working-order read timeouts recovered on attempt three within the native 120-second budget. Requests were GET-only. Literal cash available for planning is **$115,930.67**, pending cash reserves **$0**, working orders **0**, and held/pending shares **0 for all eleven configured stocks**. The higher $122,534.66 broker capacity was not substituted for literal cash. Account equity is $128,068.07 and recorded gross exposure $12,137.40; zero configured-stock holdings does not describe every account asset. Ownership is account-matched and `OBSERVED_CONSISTENT`, without new reconciliation, allocations or blocks.

This plan has **154 hourly price points**: all fourteen clocks are available for **ten symbols, including IONQ**. **PATH alone** has fourteen `UNAVAILABLE_REFERENCE_PRICE` points, from `UNDEFINED_NATIVE_PRICE_OBSERVATIONS` in the September 15 reference. Its last candidate clock is 16:48 Pacific, twelve minutes before the boundary; no valid reference price was supplied. Consequently the shared cash/share projection remains **`UNAVAILABLE_PRICE_REFERENCES`**, with no chronological events, hourly cash ledger or ending cash/holdings projection. This is unavailable projection data, not a forecast of zero trades or zero ending holdings.

Three current closing anchors use explicitly synthetic, same-session carries to September 15 17:00 Pacific:

| Symbol | Original observed close | Price | Carry minutes / synthetic rows |
|---|---|---:|---:|
| COST | September 15 16:34 Pacific | $903.03 | 26 |
| CROX | September 15 13:01 Pacific | $111.48 | 239 |
| TWST | September 15 16:13 Pacific | $137.01 | 47 |

The **312 saved synthetic minute rows** are marked synthetic, zero-volume and constant OHLC. Native source-coverage evidence is recorded for each carry; original observations remain distinct from effective boundary clocks. The 120-session historical planning reference inventory records 937 observed, 288 synthetic and 106 unavailable references. These are planning assumptions; saved metadata states native prices, model-training prices and regular-session prices were not modified. Working prices use v3 paths with ±20-basis-point conditional fill allowance; they are not guaranteed fills or live order limits.

The separate scheduled preview records 198 `NO_BULLISH_ENTRY_SIGNAL`, 55 non-entry context rows and eleven weekly `FORECAST_NOT_PROMOTED` rows. Weekly model status remains `RESEARCH_NOT_PROMOTED`, with five saved bullish and six bearish directions. Those preview exclusions must not be read as evidence that the separately selected manual Gameplan policy skips its eleven weekly instructions; live decisions retain their own current cash/holdings/quote and ownership checks. This audit did not relabel models or simulate execution.

Evidence: [compact audit JSON](C:/dev/ducketz/artifacts/analysis/overnight-20260916/trade-plan-audit.json), [readable current Gameplan](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260916T060421.076865Z/Gameplan.md), [snapshot](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260916T060421.076865Z/account-snapshot.json), [reference completion](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260916T060421.076865Z/planning-reference-completion.json). Root retains full final verification. No price-path recomputation, provider/broker calls, claims, process/control changes or production edits were performed.
