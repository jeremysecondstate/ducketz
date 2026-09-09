# Monitoring and recovery

> September 8 daytime repair: the existing operations watch is now named
> **Loops Operations Watch** and covers both daytime incidents and overnight
> preparation. See [Independent stock horizons](INDEPENDENT_STOCK_HORIZONS.md#broker-recovery-and-operational-supervision)
> for bounded broker-read retries, persistent session health, failure reporting,
> and controlled repair/restart requirements. The older daytime monitoring
> description below does not limit the user's explicitly authorized repairs.

> Current overnight supervision (September 8, 2026): the 21:05 Pacific daily
> Scheduled owner runs the full close fetch and downstream workflow, with native
> weekend/holiday no-ops. It reads training progress/errors throughout its run.
> `Loops Operations Watch` checks every 30 minutes and covers abandoned/failed
> runs; it may start a missing fresh run only after 21:15 Pacific. See
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

1. each of the six stages completed in order;
2. every required OPRA `definition`, `cbbo-1m`, and `ohlcv-1h` cursor covers the
   latest required completed session for every configured symbol;
3. Loop B and Strategy inputs are checksum compatible;
4. four model reports exist for `1h`, `4h`, `1d`, and `1w`;
5. the gameplan contains exactly 24 forecasts and 24 options intents per symbol
   in its saved manifest (168 of each for seven symbols);
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

- `Loops Overnight Gameplan`: active, daily 21:05 America/Los_Angeles, with native
  non-session no-ops and provider coverage checks.
- Standalone OPRA history: paused.
- Options Strategy paper tracking: paused.
- Stock daily adaptation: repurposed as `Loops Operations Watch`, active at :00
  and :30 each hour; missing fresh overnight starts are eligible after 21:15 PT.
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
