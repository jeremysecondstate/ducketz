# Actuals review saved-close compatibility failure

Read-only reproduction at **2026-09-16 06:09:18 UTC** identifies a writer/reader contract mismatch in the failed actuals stage. The September 15 saved trade plan is intact and source-bound; its receipt/manifest/planning-path checksums and original Gameplan receipt binding all match.

The path declares **`conditional-hourly-planning-price-path-v3`**, with sparse-reference completion v2 and trade-plan schema v4. Its native writer deliberately labels **every 17:00 point `planning_close`**, including observed-only and unavailable estimates. The pre-repair actuals reader requires **`observed_close`** at 17:00 regardless of the declared path version.

Exactly **11 fields mismatch**, one per symbol at 17:00, all solely `endpoint_kind`. All **154** symbol/date/clock/timestamp identities match and no point is missing. The first rejection is:

| Field | Saved | Reader expected |
|---|---|---|
| Key | AAPL\|2026-09-15\|17:00 | same |
| Timestamp | 2026-09-16 00:00 UTC | same (17:00 Pacific) |
| Endpoint kind | `planning_close` | `observed_close` |
| Saved low / midpoint / high | 332.42 / 333.10 / 333.77 | valid saved range |

That AAPL point has **120 observed-only samples**, zero synthetic-close samples and an observed reference. Thus `planning_close` identifies the v3 planning method, not necessarily an individual synthetic observation.

The same immutable path correctly contains **126 available estimates and 28 `UNAVAILABLE_REFERENCE_PRICE` points** for PATH/IONQ. All unavailable planned low/mid/high values are null, and the overall projection is `UNAVAILABLE_PRICE_REFERENCES`. Both unavailable 17:00 points also carry the legitimate v3 `planning_close` label; the old reader rejects labels before considering availability.

A compatible reader should recognize the declared v3 close label while preserving strict symbol/date/clock/timestamp/source checks and the existing observed-close actual selection with five-minute tolerance. Unavailable estimates must stay null, allowing observed actuals without inventing a price comparison. Preserve the original range and timestamps, and never substitute planning carries for actual market observations. Root owns implementation and tests; this subtask made no production changes or provider/broker calls.

Evidence: [compact reproduction JSON](C:/dev/ducketz/artifacts/analysis/overnight-20260916/actuals-planning-close-diagnosis.json), [read-only reproducer](C:/dev/ducketz/artifacts/analysis/overnight-20260916/diagnose_actuals_planning_close.py), [saved path](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260915T054710.644968Z/planning-price-path.json), [native writer](C:/dev/ducketz/ml/gameplan_price_bands.py:469), [failed native log](C:/DATASTORE/ml/overnight-runs/20260916T040757.217937Z/gameplan_actuals_review.log).
