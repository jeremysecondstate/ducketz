# September 23 output review

Read-only review completed 2026-09-23. All 18 independent output checks passed; detailed timestamps and evidence are in [output-review.json](C:/dev/ducketz/artifacts/analysis/overnight-20260923/output-review.json). Root's separate verifier covers full manifest hashes and cash/share conservation. No broker/provider calls, production writes or immutable-output changes were made by this review.

[September 23 Gameplan](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260923T054803.564321Z/Gameplan.md) is COMPLETE and pins `ml/nightly-gameplan-runs/20260923T054513.490247Z`. Its augmented parquet has 264 rows, exactly 24 for each of 11 symbols. The price path has all 154 hourly points AVAILABLE, covering 04:00–17:00 per symbol, at one fixed observation timestamp of 05:48:24 UTC. All 209 entry price bands are available; 55 outlook rows are NOT_ENTRY. Each stock's readable table places Projected Trade Quantity immediately before Direction Based Trade Qty, followed by trade-price range, cash, shares and action.

The read-only account snapshot at 05:48:14 UTC records literal cash of **$40,346.79**, zero reserved cash, zero working orders and consistent matching-account ownership without reasons. It does not use the larger $99,621.18 broker buying-capacity figure. Holdings are AAPL 19, AMZN 32, COST 2, CROX 0, GOOG 28, IONQ 159, MU 8, NVDA 30, PATH 467, SNDK 6 and TWST 77. Broker quote observation times remain explicitly displayed rather than being presented as fresh quotes.

The readable plan discloses all three current synthetic planning anchors, with zero assumed volume and original observation times retained:

| Symbol | Last observed close | September 22 observation, Pacific | Effective boundary | Synthetic minutes |
| --- | --- | --- | --- | --- |
| CROX | $125.00 | 13:31 | 17:00 | 209 |
| PATH | $13.26 | 16:42 | 17:00 | 18 |
| TWST | $164.55 | 16:46 | 17:00 | 14 |

These are 241 explicit synthetic bars under the saved `sparse-session-planning-reference-completion-v2` policy with a 240-minute maximum. Verified source-window evidence is saved for each. Historical planning pairs also disclose closing carries. Native prices, model-training prices and regular-session observations were not modified. Actuals retain the independent five-minute observed-price rule. The current persisted contracts were confirmed by root; this review preserves those contracts and does not substitute the older recurring-prompt policies.

The saved direction policy is `stock-direction-50-v2`, with the separate saved holding policy `accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1`. Its conditional ledger contains **80 events: 26 bearish sales and 54 bullish buys**, plus 14 hourly balances. Conditional closing cash is **$111,508.78–$112,538.94**, base **$112,025.20**. Projected remaining shares are GOOG 5, MU 2 and SNDK 2; other symbols are zero. A $2,017.34 cash buffer is preserved. There are no expiry-sale events under this saved holding policy. The no-fill baseline retains $40,346.79 and all starting shares. These are conditional transactions and proceeds, not broker fills or spendable proceeds; missed fills require recalculation.

[September 22 results](C:/DATASTORE/ml/gameplan-actuals-review-runs/20260923T055028.498627Z/Gameplan-results.md) is COMPLETE and links the original preopening Gameplan and trade plan. The original publication was saved at 05:51:37 UTC and its trade plan at 05:54:45 UTC, both before the September 22 11:00 UTC opening. All 154 saved low/mid/high price estimates match the original price path exactly, and all frozen forecast identity, probability, direction, model status and target-window fields match the original forecast parquet.

Actuals cover **165 evaluated forecasts, 33 mature forecasts missing eligible observations, and 66 pending maturity**. Missing mature forecasts are COST 3, CROX 10, IONQ 1, PATH 8 and TWST 11. Prices include **137 comparisons and 17 missing observations**: CROX 6, PATH 4 and TWST 7. Every accepted forecast boundary and price comparison is within 300 seconds of its required observation boundary. The 165 scored calls have 84 correct and 81 incorrect outcomes; missing and future outcomes are excluded. Exact missing clocks and reasons are preserved in the JSON review.

Tomorrow's readable Gameplan links the [dated September 22 results](C:/DATASTORE/ml/gameplan-actuals-review-by-date/2026-09-22/Gameplan-results.md). Both completed receipts report **zero orders**. The results explicitly describe actual market prices, not broker fills or realized trading P/L.
