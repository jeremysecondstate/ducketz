# September 21 Gameplan trader recovery

The user explicitly instructed: “unblock it and get the trader active” and “fix it and let the trading happen as per our Gameplan.” This authorizes the targeted trading-evidence repair and recovery described here. It supersedes the earlier automation-only restriction for this repair; it does not change forecast, quote, ownership, risk, or submission checks.

## Defect and tested repair

The broker described the existing CROX weekly BUY for 52 shares as CANCELED, with zero fills. Its activity used `activityType=EXECUTION`, `executionType=CANCELED`, quantity 52, and price zero. The old parser treated that cancellation leg as a fill, rejected its zero price, and left the ledger reservation WORKING. At the opening, the worker repeatedly returned `HORIZON_BROKER_RECONCILIATION_UNAVAILABLE` / `INVALID_BROKER_NUMERIC_EVIDENCE`.

`ml/stock_trader/horizon_broker.py` now separates explicit canceled quantities from real fills. Cancellation evidence must match the terminal status, exact leg, whole unfilled quantity, zero price, zero remaining quantity, and valid observation timestamps. Real fill prices remain strictly positive and must sum to the broker's cumulative filled quantity. Unknown or contradictory evidence remains rejected. Partial fills remain preserved; a cancellation releases only its unfilled remainder.

Validation:

- Broker and native ledger regression suites: 86 passed.
- Independent runtime/session, Gameplan direction runtime, execution and deployment regression suites: 123 passed.
- Independent code review found no blocking issues.
- Fresh exact matching-account broker order history at 12:13:27 UTC confirmed the cancellation; the repaired parser returned VERIFIED / CANCELLED / zero filled / zero remaining.

## Controlled recovery

Supervision owner `f85c1328-b354-4404-a158-34284ed5a476` was acquired and renewed. The original worker's commands, process identities, session lock, activation files, and entry/recovery claims were saved before the restart. Only the Gameplan-specific activation intent was temporarily set false through its native writer; the global stock control was untouched. The old worker published `STOPPED_TRADER_INACTIVE` at 12:14:47 UTC and both original Python processes exited, releasing their native locks.

Maintenance used the existing per-cycle trader lock, a SQLite backup, native exact-order capture, a newer complete account/open-orders/quote snapshot, and `HorizonLedger.reconcile` with zero execution budgets. There were no SQL repairs, order API mutations, synthetic fills, or live allocations created by maintenance. Two Schwab open-order GET timeouts occurred before reconciliation; the existing native bounded full-snapshot retry was used without changing the provider range, timeout, coverage, or freshness rules. The successful complete snapshot reconciled the sole canceled reservation, returned READY with no reasons and zero pending reservations, and preserved all filled shares and saved entry claims.

The original enabled control contents were restored exactly. The existing launcher was restarted hidden with the same `gameplan-direction-current-market-v1`, `--execute`, all horizons, `--run-session`, and `--wait-for-open` configuration. No late-opening or quote-recovery flag was used, and no schedule was changed. Replacement owner: PowerShell launcher 40600, virtualenv launcher 13464, worker 42576.

At 12:18:14 UTC / 05:18 Pacific, the replacement reported RUNNING with a CURRENT broker snapshot, no errors, zero consecutive failures, and five orders submitted under the existing Gameplan execution rules. This is broker submission evidence, not a claim that all orders filled. Subsequent status and saved decision/ledger evidence are recorded in the completion verification.

## Preserved boundaries

No forecasts, model statuses, source identities, price rules, entry windows, risk limits, or trading strategy were changed. Existing holdings and execution identities remain native. The prior overnight run is still failed at trade planning; its original Monday 04:00 deadline has elapsed and no late publication exception was invented. This recovery fixes the user-authorized live trader; it does not relabel the unfinished overnight reports as complete.

Evidence: `before-stop.json`, `stop-request.json`, `holdings-before.sqlite3`, `broker-order-status-readonly.json`, `native-reconciliation.json`, `restored-intent.json`, `restarted-launcher.json`, and `completion-verification.json` in this directory.

Final verification at 2026-09-21T12:21:10Z: replacement worker RUNNING through six cycles, zero failed/consecutive-failure cycles, five submitted orders with all five broker-confirmed FILLED (GOOG5, MU1, NVDA8, SNDK1, TWST11; all BUY, 1h). No duplicate execution identities, pending reservations or persistent ownership blocks. Source remains pinned September21 YG20260919T061628.240412Z. Original activation contents restored, latest native inventory ready. completion-verification.json records these checks. The worker continues normal session management through17:00Pacific.
