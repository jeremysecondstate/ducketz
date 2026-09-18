# September 18 YG quality-revision output audit

Reviewed 2026-09-18T08:08:17.655337+00:00. All 764 bounded saved-output checks passed.

The native three-stage publication, enrichment and trade-planning run is COMPLETE, retaining the original September 18 04:00 Pacific deadline (11:00 UTC) without an exception. The [YG Gameplan](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260918T080320.581880Z/Gameplan.md) binds publication `20260918T080016.713533Z`, variant YG and `raw-price-direction-v1`.

The same eleven configured stocks have 264 forecasts, 264 stock-only intents and 264 augmented trade-plan rows. Every original forecast column is retained exactly. All 154 hourly price points are AVAILABLE. Output hashes, receipt/manifest links, source bindings and current pointers passed.

The conditional scenario has 71 events (54 buys, 17 bearish sales). Starting cash is $115,997.00; ending cash is $49,775.85–$50,574.57 (base $50,177.05). Every event's before/change/after cash and shares, all fourteen hourly balances and ending balances conserve. Holding policy: `accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1`.

Ending conditional shares: AAPL 19, AMZN 25, COST 0, CROX 52, GOOG 18, IONQ 160, MU 7, NVDA 30, PATH 465, SNDK 5, TWST 41.

The no-fill baseline remains $115,997.00 and the snapshot holdings. Snapshot time: 2026-09-18T08:03:25.089332+00:00; working orders: 0; reserved cash: $0.00. Projected fills and sale proceeds remain conditional; these are not broker executions.

Original price observations match the earlier September 18 review. CROX alone retains the explicit $123.19 close observed September 17 at 16:53 Pacific, carried seven minutes through 17:00. The seven synthetic bars are identical, zero volume, and disclosed as assumptions within verified same-session coverage. Native prices and training prices remain unmodified.

The OG receipt and manifest are unchanged (receipt SHA-256 `ba53f5f4b62443c426190da8cad96cf7e18644ee8c3f278b0fa5092c93b3de97`). OG remains preserved for evaluation. This explicitly narrower run ends after trade planning; it does not add or rerun the prior-session actuals review.

All four directional model gates and all 264 forecast promotion/exact-route-support checks pass. Frozen model statuses are {"PROMOTED": 264}. Optional learned enrichment qualification remains separate. Current direction policy is stock-direction-50-v2. 46 immutable baseline file hashes match OG, first YG, first YG trade plan and first ACTIVE deployment.

All native, publication and trade-plan receipts report zero orders. No provider calls, broker fetches, trading operations, training, claims or production writes were made by this audit.

Structured checks: [output-review.json](C:/dev/ducketz/artifacts/analysis/yg-20260918/quality-followup/output-review.json).
