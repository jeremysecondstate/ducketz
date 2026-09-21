# September 21 Gameplan: cancellation parser repair requires authorization

The September 20 scheduled wake adopted diagnosis of the existing Friday-source attempt after acquiring its own supervision claim. It did not start a new weekend pipeline or retry the unchanged failure.

Native attempt `C:/DATASTORE/ml/overnight-runs/20260919T040748.657031Z` completed six stages, then failed `gameplan_trade_planning` with `OWNERSHIP_SNAPSHOT_UNAVAILABLE`. `gameplan_actuals_review` has not started. The pinned YG publication is `20260919T061628.240412Z`, action September 21, original deadline September 21 at 04:00 Pacific (`2026-09-21T11:00:00Z`). No exception applies.

## Verified current evidence

- The eleven-symbol watchlist matches the batch's ACTIVE activation receipt. No onboarding replay or candidate-state mutation is needed.
- Failed native report and all seven log hashes match the terminal receipt. All saved Gameplan output size/hash bindings pass. There are exactly 264 forecasts and 264 stock-only intents, 24 of each per symbol. All four directional models and all rows retain PROMOTED status under their own v2 policy. Prior exact metric reproduction is retained: all four models pass tolerances but none beats its baseline.
- Four enrichment fits remain source-bound; qualified scopes are zero for every horizon. These optional learned models do not determine the selected manual Gameplan quantities.
- Publication records all 33 production OPRA cursors through exclusive September 19. The prior source-bound provider audit verifies 33 zero-dollar/capacity-checked scopes, all current partition hashes and zero provider failures. No acquisition was repeated.
- Fresh exact tracked-order history at September 20 04:07:36 UTC matches the ledger account and broker order: CROX weekly BUY 52 is CANCELED, zero filled, zero remaining. The ledger still retains its WORKING reservation. Broker absence from open orders was not used as cancellation proof.
- A newer complete read-only snapshot at 04:08:38 UTC reports cash $58,034.37, zero working orders, and holdings exactly matching native owned totals. The only ownership reason is `PENDING_LEDGER_RESERVATIONS_REQUIRE_RECONCILIATION`. Existing holdings, including overdue one-share MU/SNDK hourly allocations, remain untouched.
- Saturday's independent cumulative evaluation contains 4,248 saved forecasts: 3,100 evaluated, 587 mature awaiting observations, 561 pending. Its independent pointer advance is preserved.
- The current readable trade plan still refers to September 18; current actuals still refer to September 17. Neither is a completed September 21 plan / September 18 results review.

## Precisely reproduced defect

`ml/stock_trader/horizon_broker.py` `_fills` recognizes `activityType=EXECUTION` but never examines `executionType`. Schwab's explicit `executionType=CANCELED` activity includes the canceled quantity 52 and a zero-price leg. Native normalization correctly accepts the order's terminal quantities, then calls `_fills`, which calls `_number(price, positive=True)` and raises `INVALID_BROKER_NUMERIC_EVIDENCE`. An independent read-only reproduction traced this exact path. No actual fill was supplied by the broker.

Existing standalone `ml.stock_trader.reconciliation` only writes legacy execution-event snapshots; it does not reconcile the independent horizon ledger. Research ownership preparation rejects open reservations. The only production horizon reconciliation caller is the active trader runtime, which encounters this same defect. Starting that worker is outside scope and would not repair the parser.

## Concrete proposed work, not applied

1. In the order-evidence parser, distinguish explicit cancellation activity from actual fills. Preserve strict positive prices for real fills, exact order/account/symbol/leg identity, unknown-status rejection, and cumulative-fill equality. Do not accept zero-price fills or discard arbitrary activity data.
2. Add regression coverage for this exact cancellation payload; partial fills followed by cancellation; missing/unknown/contradictory activity records; invalid genuine fill prices; identity mismatches; and coherent newer portfolio/idempotent reconciliation.
3. Provide a bounded maintenance path using the existing native capture/ledger reconciliation methods under supervision and stock-trader locks. It must have no order submission/cancellation/replacement capability, verify fresh exact terminal order evidence and a newer matching-account portfolio, preserve all fills/owned allocations/unknown submissions, and release only evidence-confirmed unfilled reservations. No direct SQL repair.
4. Once ownership is safe, verify the existing failed terminal attempt and resume only planning then actuals, with the pinned YG source, original price contract and unchanged Monday 04:00 deadline. Verify all outputs and current pointers before claiming completion.

The automation explicitly permits focused **non-trading repairs** and requires preserving **all trading controls/code**. This repair is in trading evidence code and reconciliation changes operational ledger evidence, so it requires explicit authorization before application. This is the user's scope boundary, not an automatic approval-review rejection. No code/ledger repair or order action has been performed.

Evidence in this directory: `broker-order-status-readonly.json`, `account-snapshot.json`, `verification.json`. Prior detailed model/provider evidence remains under `C:/dev/ducketz/artifacts/analysis/overnight-20260919`.
