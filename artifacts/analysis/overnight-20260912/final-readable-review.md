# Final readable Gameplan and prior actuals review

Reviewed 2026-09-12T05:20:03.944821+00:00. Bounded offline review; production artifacts unchanged. Full native recomputation and receipt audit remain the parent audit.

Current plan: [20260912T051328.518994Z](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260912T051328.518994Z/Gameplan.md). Prior actuals: [20260912T051439.995563Z](C:/DATASTORE/ml/gameplan-actuals-review-runs/20260912T051439.995563Z/Gameplan-results.md).

September 14 plan has all seven required adjacent-column stock tables. Nonentry gap/future outlook rows retain dashes for quantities. All 168 directional forecast rows are promoted; learned sizing is still explicitly a separate research assessment lane. The projection contains three daily buys at 04:00 PDT (GOOG 17, AAPL 17, AMZN 22 shares) and their 17:00 exits. Zero orders were submitted. The text clearly describes estimates, shared cash, protected horizon holdings, live recalculation, fees/taxes exclusions, and the no-fill baseline.

Current anchor evidence has zero synthetic bars and seven AVAILABLE_OBSERVED references. Six stocks use September 11 17:00 PDT; NVDA uses its observed 16:55 close, exactly within the native five-minute tolerance. Its source minute starts at 16:54. No missing synthetic disclosure is required for this run. This differs from the prior September 11 plan, whose linked original document explicitly discloses COST $902.20 observed September 10 16:47, synthetically carried 13 minutes to 17:00. That planning assumption is not an actual endpoint in the September 11 review.

The September 11 actuals contain 119 evaluated forecasts, 42 pending target maturity and seven mature forecasts waiting for data. Hourly prices contain 95 comparisons and three missing observations. Every mature missing item is COST. Actuals explicitly use observed prices under the existing five-minute boundary rule, do not fill missing values, and distinguish market comparisons from broker fills or P/L. A missing observed endpoint here does not independently establish a provider failure.

| COST route | Pacific target window | Missing endpoint |
| --- | --- | --- |
| 1h@04:00 | 2026-09-11 04:00 PDT to 2026-09-11 05:00 PDT | end |
| 1h@13:00 | 2026-09-11 13:00 PDT to 2026-09-11 14:00 PDT | end |
| 1h@14:00 | 2026-09-11 14:00 PDT to 2026-09-11 15:00 PDT | start and end |
| 1h@15:00 | 2026-09-11 15:00 PDT to 2026-09-11 16:00 PDT | start and end |
| 1h@16:00 | 2026-09-11 16:00 PDT to 2026-09-11 17:00 PDT | start |
| 1h@gap | 2026-09-10 17:00 PDT to 2026-09-11 04:00 PDT | start |
| 4h@12:00 | 2026-09-11 12:00 PDT to 2026-09-11 16:00 PDT | end |

Missing hourly price comparisons are COST September 11 14:00, 15:00 and 16:00 PDT. The 1h@04:00 forecast lacks its closing endpoint at 05:00; the hourly 05:00 price comparison uses an opening observation and can be available independently.

The 42 pending forecasts are six routes for each of AAPL, AMZN, COST, GOOG, MU, NVDA and SNDK:

| Route | Pacific target window |
| --- | --- |
| 4h@16:00 | 2026-09-11 16:00 PDT to 2026-09-14 07:00 PDT |
| 1d@D+2 | 2026-09-14 04:00 PDT to 2026-09-14 17:00 PDT |
| 1d@D+3 | 2026-09-15 04:00 PDT to 2026-09-15 17:00 PDT |
| 1d@D+4 | 2026-09-16 04:00 PDT to 2026-09-16 17:00 PDT |
| 1d@D+5 | 2026-09-17 04:00 PDT to 2026-09-17 17:00 PDT |
| 1w@D+5 | 2026-09-11 04:00 PDT to 2026-09-17 17:00 PDT |

All three local document links resolve. The September 14 Gameplan links the dated September 11 actuals, whose current pointer selects this immutable actuals run and whose readable copy matches it byte-for-byte. Actuals link the original September 11 forecasts and trade plan, with successor action date September 14.

Confirmed native v4 publication uses ml/gameplan_trade_planning.py:339 _plan_working_price_rows(forecasts,snapshot,bands,price_path,policy), which at line205 explicitly recomputes standalone capacity at the conditional working prices and preserves the historical stress band separately. The root audit correction matches this contract.

Evidence: final-readable-review.json, native planning-reference-completion.json, empty synthetic-reference-bars.parquet, forecast-results.parquet, price-results.parquet, and the two readable documents.
