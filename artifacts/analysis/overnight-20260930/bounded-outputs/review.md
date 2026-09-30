Bounded trade-plan and actuals review completed at 2026-09-30T06:40:08.761494+00:00.

PASS: 38 trade-plan checks and 36 actuals checks. Native run 20260930T040723.733025Z was COMPLETE before output reads. These checks use saved receipts, manifests, reports and bounded publication tables; no raw archive reconstruction, broker/provider calls or production changes occurred.

The September 30 publication has 264 forecasts, 264 stock-only intents and 264 augmented trade rows (24 per symbol across eleven symbols), with all 154 hourly planning prices available. Decimal arithmetic independently conserves cash and shares across 32 hypothetical events (13 sales and 19 buys), all 14 hourly summaries and eligible forecast rows. Literal initial cash is $54.85; conditional ending low/base/high cash is $27.17 / $180.87 / $335.15, before fees and taxes. The unchanged no-fill baseline and saved real holdings match the post-reconciliation baseline.

Two current planning anchors use explicitly synthetic zero-volume bars: CROX $123.00, observed September 29 at 13:40 PDT, carried 200 minutes; PATH $12.31, observed 15:14 PDT, carried 106 minutes. Both end at that session’s 17:00 boundary. All 306 bars preserve original observation times, source identity, gap and coverage; the readable Gameplan discloses both anchors. This verifies the saved/current v3 and sparse-session-v2 240-minute planning policy. It does not claim compliance with the older v2 15-minute rule. The saved YG direction threshold is 50 percent and holdings persist until a bearish signal, as documented in the current operating contract.

The September 29 actuals use the original Gameplan published at 06:23:01Z and trade plan completed at 06:28:25Z that day, before the 11:00Z opening. Frozen forecasts, original ranges/midpoints and price-path clocks remain exact. Outcomes stop at the completed session’s close. There are 163 evaluated forecasts, 35 mature forecasts lacking eligible observations and 66 pending forecasts. Direction accuracy is 84/163 (51.53%) under the frozen policy; pending/missing results are excluded and cost-adjusted targets remain separate.

Hourly actuals contain 135 compared prices and 19 missing prices (15 outside the five-minute tolerance and four with no observation). Seventeen of 135 compared prices fall within the saved range. Missing values remain null even where saved request intervals verify source coverage. Raw native data was not reloaded here; the main final audit separately verifies payloads and endpoint selection.

Missing mature forecasts: COST 5, CROX 11, GOOG 2, IONQ 1, PATH 8, TWST 8. Pending: eleven each for 4h@16:00, 1d@D+2, 1d@D+3, 1d@D+4, 1d@D+5 and 1w@D+5.

Missing hourly actual prices:
- COST: 06:00, 14:00 Pacific.
- CROX: 04:00, 05:00, 06:00, 14:00, 15:00, 16:00, 17:00 Pacific.
- PATH: 05:00, 14:00, 15:00, 16:00, 17:00 Pacific.
- TWST: 05:00, 06:00, 14:00, 15:00, 16:00 Pacific.

Receipt/manifest bindings, dated actuals output and the successor’s results link pass. Saved outputs assert zero orders and review-only authority. The root preservation audit supplies the separate operational-ledger mutation check. No actual broker fills or realized P/L are implied.

Detailed evidence:

- [Trade-plan review](C:/dev/ducketz/artifacts/analysis/overnight-20260930/bounded-outputs/final-output-review.json)
- [Actuals review](C:/dev/ducketz/artifacts/analysis/overnight-20260930/bounded-outputs/actuals-coverage-review.json)
- [Bound summary and hashes](C:/dev/ducketz/artifacts/analysis/overnight-20260930/bounded-outputs/bounded-output-summary.json)
- [Readable September 30 Gameplan](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260930T062633.629638Z/Gameplan.md)
- [September 29 results](C:/DATASTORE/ml/gameplan-actuals-review-runs/20260930T063012.679198Z/Gameplan-results.md)
