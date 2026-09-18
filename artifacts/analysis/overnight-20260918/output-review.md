# September 18 final output review

Reviewed 2026-09-18T05:55:09.902796+00:00. Offline inspection of completed, matching outputs; full source/conservation rederivation is delegated to the root verifier.

The [September 18 Gameplan](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260918T054811.145288Z/Gameplan.md) is COMPLETE and matches pinned Gameplan `20260918T054532.489998Z`. It has 264 augmented rows, 24 for each of 11 stocks, and 154/154 available hourly price points (04:00–17:00). No UNAVAILABLE_REFERENCE_PRICE, UNAVAILABLE_MINIMUM_SAMPLES or UNAVAILABLE_PRICE_REFERENCES result remains. The minimum historical price-point pair count is 8.

The signal-driven projection contains 33 conditional transactions: 30 bullish buys and 3 bearish sales. Starting available cash is $115,997.00; ending cash is $6,031.49–$6,573.29 (base $6,304.06), retaining the $5,799.85 buffer. The 14 portfolio clocks and 33 ordered events retain before/change/after balances. The current holding policy accumulates bullish shares and sells on bearish signals; it has no scheduled expiry exits.

Ending conditional shares: AAPL 30, AMZN 15, COST 4, CROX 153, GOOG 25, IONQ 256, MU 9, NVDA 47, PATH 743, SNDK 7, TWST 89. The no-fill baseline remains $115,997.00 and 1 NVDA share. These are conditional planning balances, not actual spendable sale proceeds or broker fills.

The read-only account snapshot is `2026-09-18T05:48:54.296720+00:00`, with zero working orders and zero reserved cash. The planning price observation cutoff is `2026-09-18T05:49:03.543068+00:00`. All output receipts report zero orders.

The only current synthetic anchor is CROX: observed close $123.19 at September 17 16:53 Pacific (23:53Z), carried 7 minutes to 17:00 Pacific (September 18 00:00Z). Seven explicit zero-volume synthetic bars are ASSUMED_NO_TRADES. Native verified CROX coverage spans September 15 00:00Z through September 18 00:00Z and was published at 05:43:56.718908Z before the planning cutoff. The original timestamp is retained. IONQ uses an observed 16:56 close; PATH and TWST use observed 16:58 closes; all other symbols use observed 17:00 closes. Native archives and training prices remain unchanged.

The [September 17 actuals review](C:/DATASTORE/ml/gameplan-actuals-review-runs/20260918T055047.136335Z/Gameplan-results.md) selected the original pre-open Gameplan `20260917T054622.686474Z` and trade plan `20260917T054857.515593Z`. Its 264 forecasts contain 164 evaluated, 34 mature awaiting observations and 66 future windows. Missing matured forecasts: CROX 11, TWST 9, COST 8, PATH 5, IONQ 1. Each stock has six future rows: 16:00 four-hour, daily D+2 through D+5, and weekly.

Of 154 saved hourly price estimates, 137 were compared and 17 await an observation within five minutes. Missing price points: CROX 7, TWST 5, COST 3, PATH 2. All 17 have OUTSIDE_TOLERANCE diagnostics with VERIFIED_COMPLETE native source-request coverage. These are observation gaps; unchanged re-fetching is not justified by those diagnostics.

Raw-price direction correctness is 62/164 (37.80%), excluding pending/missing results. 11/137 compared prices are inside the saved range. This is separate from the cumulative evaluator's cost-adjusted model score. The 34 mature missing forecasts contain 24 missing start-boundary occurrences and 23 missing end-boundary occurrences; all are OUTSIDE_TOLERANCE with VERIFIED_COMPLETE source ranges. Repeated boundaries are counted once per affected forecast endpoint.

Cumulative evaluation since September 4 contains 3,192 saved forecasts: 2,543 evaluated, 470 mature awaiting data and 179 pending maturity. Each historical plan remains evaluated against its own saved universe and source. The saved summary reports 60.20% model direction accuracy and 0.23430 mean Brier score; September 17 alone is 164 evaluated, 34 missing and 66 pending.

Structured evidence: [output-review.json](C:/dev/ducketz/artifacts/analysis/overnight-20260918/output-review.json). No errors detected in this bounded output inspection.
