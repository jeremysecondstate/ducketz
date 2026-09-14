# Monitoring and recovery

Research-selected additions use [Research symbol onboarding](RESEARCH_SYMBOL_ONBOARDING.md): an explicitly selected batch, a 2018 historical floor, included-plan cost checks, candidate training, verified publication, and atomic activation. Read current membership from `datafetching/watchlist.txt` and validate historical publications against their own saved universes.

> September 8 daytime repair: the existing operations watch is now named
> **Loops Operations Watch** and covers both daytime incidents and overnight
> preparation. See [Independent stock horizons](INDEPENDENT_STOCK_HORIZONS.md#broker-recovery-and-operational-supervision)
> for bounded broker-read retries, persistent session health, failure reporting,
> and controlled repair/restart requirements. The older daytime monitoring
> description below does not limit the user's explicitly authorized repairs.

> Current overnight supervision (September 13, 2026): the 21:05 Pacific daily
> Scheduled owner runs the full close fetch and downstream workflow, with native
> weekend/holiday no-ops. It reads training progress/errors throughout its run.
> `Loops Operations Watch` checks every 90 minutes and covers abandoned/failed
> runs; it may start a missing fresh run only after 21:15 Pacific. See
> [NIGHTLY_GAMEPLAN.md](NIGHTLY_GAMEPLAN.md) for status, stop, recovery, repair,
> and resume. The monitor/guardian implementations described below are diagnostic
> components; the current Scheduled owner follows that supervision procedure.


## Current deployment

The former hourly monitor/adaptive-trainer automation is retired. The old eight
recurring processes are stopped, so their absent PIDs are expected and must not
trigger guardian recovery.

Current monitoring follows one overnight run and its separate forecast and
trade-review publications:

- stage receipts under `C:\DATASTORE\ml\overnight-runs`;
- final pointer `C:\DATASTORE\ml\nightly-gameplan-latest\run.json`;
- trade-review pointer `C:\DATASTORE\ml\gameplan-trade-plan-latest\run.json`,
  pointing to the same action date and pinned source Gameplan;
- selected generation manifest/receipt and exact row counts;
- all `3 × configured symbols` production OPRA cursors (33 for eleven symbols);
- optional daytime paper receipts under
  `C:\DATASTORE\ml\gameplan-decision-runs`;
- live stock decisions under `C:\DATASTORE\ml\stock-trader-decision-runs` and
  their execution/reconciliation events.

## Nightly success checks

A successful run must prove:

1. every stage selected in the run completed in order; new full independent
   runs include both `stock_enrichment_training` and the required
   `gameplan_trade_planning` tail, while older explicit endpoints remain intact;
2. every required OPRA `definition`, `cbbo-1m`, and `ohlcv-1h` cursor covers the
   latest required completed session for every configured symbol;
3. Loop B and selected downstream inputs are checksum compatible; optional
   Strategy stages are omitted only under the explicit stock-only scope;
4. four model reports exist for `1h`, `4h`, `1d`, and `1w`;
5. the gameplan contains exactly 24 forecasts and 24 options intents per symbol
   in its saved manifest (264 of each for eleven symbols);
6. the pointer, manifest, receipt, and file checksums verify;
7. forecasts remain advisory and overnight `orders_placed=0`;
8. the separate trade-review has 24 rows per configured symbol, exact original
   probabilities/windows/source identity, account cash and holdings timestamps,
   zero submitted orders and valid receipt/output checksums;
9. Projected Trade Quantity equals standalone full horizon capacity bounded by
   current cash/exposure and the upper working price. Adjacent Direction Based
   Trade Qty agrees with signed BUY/SELL events in one shared chronological
   ledger, with Neutral zero and non-entry context a dash;
10. the fresh snapshot covers literal available cash and every configured stock's
    current shares, including COST, pending orders, options/other exposure and
    active allocations. Share totals reconcile without selling reserved shares
    or another horizon's holdings;
11. bearish sales, remaining due horizon exits and bullish buys occur in that
    order at each clock. All row cash/share fields match the post-batch hourly
    table; each event's proceeds/cost enters once. The 04:00–17:00 rollforward and
    EOD holdings reconcile, retaining overnight/weekly lots with later expiry;
12. `planning-price-path.json` and `direction-ledger.json` are included in native
    output verification. Conditional +/-20bps working bands surround the observed
    median, while wider historical stress ranges remain separate. Cash scenarios
    are before fees/taxes and depend on assumed fills, never reported fills.

The existing scheduled-entry preview stays expandable and retains its separate
confidence-weighted budget. The manual Gameplan executor follows the same
directions but recalculates orders from actual cash, eligible inventory and
current ask/bid quotes. Planning price/cash ranges never reject an order for
being outside an estimate. It does not copy this direction ledger or spend its
hypothetical proceeds. No-fill
baseline and assumptions must remain recorded; unavailable or different fills
require a newly calculated projection. See
[the full review contract](NIGHTLY_GAMEPLAN.md#account-aware-trade-plan-review).

Check 54%/46% direction labels independently of each saved model's approval.
New directional policy v2 permits +0.005 Brier and +0.01 log-loss differences
above baseline; verify the actual scores, remaining checks and declared policy,
without relabeling older strict-policy reports. Regularized daily logistic
challengers are chosen on development data before assessment. Learned sizing
qualification is separate and retains its own reported outcome.

The review counts the completed session as Day 1 and the upcoming session as
Day 2, showing actual dates and weekly expiry. Price evidence may use available
same-source history with at least two samples; counts and limitations remain
in the machine-readable artifacts. The main view shows working prices, adjacent
capacity/direction quantities, Plan action and remaining cash/shares, without
repeated reference qualifiers or historical-sample/status columns. Missing price evidence stays
unavailable rather than becoming invented capacity.

The final quote from a closed session can be hours old. Monitor session coverage,
not wall-clock quote age, for overnight planning. Live/order-time checks remain
separate and are not satisfied by the nightly receipt.

## Failure response

The overnight runtime fails closed at the first failed stage. A trade-planning
failure preserves the last valid trade-review pointer and the already-published
immutable Gameplan. Resume only the failed tail stage using its original pinned
Gameplan and deadline; do not rerun successful publication or enrichment.
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
  each hour; missing fresh overnight starts are eligible after 21:15 PT.
- Former intraday stock tasks: paused.
- `Loops Stock Trader — Daytime Supervision`: active at 03:55 Pacific on
  weekdays, supervising the Windows-owned independent session start.
- `Loops Gameplan Stock Trader — 1 p.m. Transition`: paused; the session worker
  handles that transition.
- Saturday operator review: active and read-only.

The independent stock session worker owns daytime stock execution and requires
both persistent activation controls plus `--execute`. Overnight trade planning
does not acquire broker order authority.

## Daytime consumers

A user may start [`Start-Gameplan-Trader.cmd`](../../Start-Gameplan-Trader.cmd)
once before the session. Recognize a valid `SLEEPING_UNTIL_OPEN` worker as healthy
standby when its PID/lock identity and <=30-second heartbeat match, its
`action_date` and `wakes_at` identify the next supported 04:00 Pacific session,
and its command selects `gameplan-direction-current-market-v1 --run-session
--wait-for-open`. Zero broker calls and zero entry-slot claims are expected
before wake; a missing unfinished nightly publication is not a sleeping-worker
failure. The process reads the current Gameplan on wake and needs no second
manual confirmation. A control switched off must terminate standby promptly.

The existing 03:55 Scheduled launcher may supervise the verified manual worker
instead of starting another. Validate matching policy and wait mode across its
launcher/child, exact executable identity and lock lifetime. Do not replace a
healthy manual worker with the fixed scheduled policy. The manual start changes
no recurrence, and the Scheduled default remains fixed sizing. Watch live
balances and confirmed fills independently of the overnight cash estimates.

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
