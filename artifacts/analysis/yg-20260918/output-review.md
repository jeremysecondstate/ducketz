# September 18 YG output audit

Reviewed 2026-09-18T07:33:57.377966+00:00. All 345 bounded saved-output checks passed.

The native three-stage publication, enrichment and trade-planning run is COMPLETE, retaining the original September 18 04:00 Pacific deadline (11:00 UTC) without an exception. The [YG Gameplan](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260918T072910.923004Z/Gameplan.md) binds publication `20260918T072555.813034Z`, variant YG and `raw-price-direction-v1`.

The same eleven configured stocks have 264 forecasts, 264 stock-only intents and 264 augmented trade-plan rows. Every original forecast column is retained exactly. All 154 hourly price points are AVAILABLE. Output hashes, receipt/manifest links, source bindings and current pointers passed.

The conditional scenario has 10 events (10 buys, 0 bearish sales). Starting cash is $115,997.00; ending cash is $54,277.51–$54,533.61 (base $54,407.07). Every event's before/change/after cash and shares, all fourteen hourly balances and ending balances conserve. Holding policy: `accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1`.

Ending conditional shares: AAPL 19, AMZN 25, COST 0, CROX 52, GOOG 18, IONQ 160, MU 6, NVDA 30, PATH 465, SNDK 3, TWST 41.

The no-fill baseline remains $115,997.00 and the snapshot holdings. Snapshot time: 2026-09-18T07:29:14.542680+00:00; working orders: 0; reserved cash: $0.00. Projected fills and sale proceeds remain conditional; these are not broker executions.

Original price observations match the earlier September 18 review. CROX alone retains the explicit $123.19 close observed September 17 at 16:53 Pacific, carried seven minutes through 17:00. The seven synthetic bars are identical, zero volume, and disclosed as assumptions within verified same-session coverage. Native prices and training prices remain unmodified.

The OG receipt and manifest are unchanged (receipt SHA-256 `ba53f5f4b62443c426190da8cad96cf7e18644ee8c3f278b0fa5092c93b3de97`). OG remains preserved for evaluation. This explicitly narrower run ends after trade planning; it does not add or rerun the prior-session actuals review.

Frozen model statuses are {"PROMOTED": 99, "RESEARCH_NOT_PROMOTED": 165}. Model fitting/inference is covered by the separate model review. Current direction policy is stock-direction-50-v2; no legacy thresholds were assumed.

All native, publication and trade-plan receipts report zero orders. No provider calls, broker fetches, trading operations, training, claims or production writes were made by this audit.

Structured checks: [output-review.json](C:/dev/ducketz/artifacts/analysis/yg-20260918/output-review.json).
