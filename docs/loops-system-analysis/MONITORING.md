# Monitoring and recovery

> Current overnight supervision (September 4, 2026): the 17:05 Scheduled owner
> reads training progress/errors throughout its run; a ten-minute health watch
> covers missed starts and abandoned/failed runs. See
> [NIGHTLY_GAMEPLAN.md](NIGHTLY_GAMEPLAN.md) for status, stop, recovery, repair,
> and resume. The monitor/guardian implementations described below are diagnostic
> components; the current Scheduled owner follows that supervision procedure.


## Current deployment

The former hourly monitor/adaptive-trainer automation is retired. The old eight
recurring processes are stopped, so their absent PIDs are expected and must not
trigger guardian recovery.

Current monitoring follows one overnight run and one immutable final pointer:

- stage receipts under `C:\DATASTORE\ml\overnight-runs`;
- final pointer `C:\DATASTORE\ml\nightly-gameplan-latest\run.json`;
- selected generation manifest/receipt and exact row counts;
- all 18 production OPRA history cursors;
- optional daytime paper receipts under
  `C:\DATASTORE\ml\gameplan-decision-runs`;
- live stock decisions under `C:\DATASTORE\ml\stock-trader-decision-runs` and
  their execution/reconciliation events.

## Nightly success checks

A successful run must prove:

1. each of the five stages completed in order;
2. every required OPRA `definition`, `cbbo-1m`, and `ohlcv-1h` cursor covers the
   latest required completed session for all six symbols;
3. Loop B and Strategy inputs are checksum compatible;
4. four model reports exist for `1h`, `4h`, `1d`, and `1w`;
5. the gameplan contains exactly 144 forecasts and 144 options intents;
6. the pointer, manifest, receipt, and file checksums verify;
7. `execution_authority=ADVISORY_PAPER_ONLY` and `orders_placed=0`.

The final quote from a closed session can be hours old. Monitor session coverage,
not wall-clock quote age, for overnight planning. Live/order-time checks remain
separate and are not satisfied by the nightly receipt.

## Failure response

The overnight runtime fails closed and preserves the prior valid final pointer.
Do not edit a pointer, delete a lock, restart the old recurring stack, or retry a
provider/model stage automatically. Record the first failed stage and use the
stage report plus referenced receipt to diagnose it.

Permitted read-only checks include:

```powershell
.\.venv\Scripts\python.exe -m ml.option_pricing_opra --datastore-target pc --health-only
.\.venv\Scripts\python.exe -m datafetching.datastore_hygiene --datastore-target pc
```

The hygiene command must not include cleanup/confirmation flags during
monitoring.

## Scheduler state

- `Loops Overnight Gameplan`: active, weekday 17:05 PT.
- Standalone OPRA history: paused.
- Options Strategy paper tracking: paused.
- Stock daily adaptation: paused.
- Former intraday stock tasks: paused.
- `Loops Gameplan Stock Trader — Hourly`: active.
- `Loops Gameplan Stock Trader — 1 p.m. Transition`: active.
- Saturday operator review: active and read-only.

Only the two gameplan stock schedules own daytime broker mutation. They are
stock-only and require both persistent activation controls plus `--execute`.

## Daytime consumers

`ml.gameplan_executor` validates the frozen action date and route. A valid
decision receipt proves only that the advisory/paper plan was read; it is not an
execution receipt. Missing current quotes or a closed option session cannot be
repaired by changing contracts intraday.

`ml.gameplan_stock_trader` writes stock decision and execution receipts. Monitor
the current action boundary, `GAMEPLAN_STOCK_ACTIONABLE_RECEIPT_VALIDATED`,
exact-once suppression, selected/submitted counts, and the subsequent read-only
reconciliation. A missed boundary is terminal and must not be replayed.

## Legacy diagnostics

`ml.system_monitor`, `ml.system_guardian`, and
`docs/datafetch-ml/start_all_loops.ps1` remain available for explicit diagnosis
of the legacy topology. Their old hourly schedule and dead-process recovery are
not current production authority.
