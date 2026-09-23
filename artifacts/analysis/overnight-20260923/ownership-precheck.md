# Overnight ownership precheck

Reviewed 2026-09-23T04:11:08.585729+00:00 for overnight run `20260923T040814.422433Z`.

The September 22 manual Gameplan session is terminal `FINISHED_WITH_ERRORS`: 1,114 calls, 49 submissions and one failed closing-boundary cycle. Its final broker capture completed at 00:00:01.790 UTC, after the 17:00 Pacific window, so the final cycle recorded `EXECUTION_WINDOW_CLOSED_AFTER_BROKER_CAPTURE` / `OUTSIDE_US_EQUITY_ACTIONABLE_SESSION`, zero selected orders and zero submitted orders. The receipt hashes verify; its inventory reconciliation was ready with no reasons.

One new reservation remains WORKING: TWST 1h SELL 38 at $166.31, broker order `1008025436225`, zero fills, last observed 2026-09-22 23:59:59 UTC. It has no saved cancellation request and no assigned existing inventory. Current broker status is unknown from this local-only precheck. Exact terminal order evidence must be captured and native reconciliation completed before planning can clear `PENDING_LEDGER_RESERVATIONS_REQUIRE_RECONCILIATION`. Absence from open orders alone cannot resolve it.

The latest saved ownership snapshot is ready, with no persistent blocks. It is end-of-session evidence, not a fresh current account snapshot. Daytime trading changed holdings since yesterday's reconciliation and added 48 filled reservations plus this one working reservation. Yesterday's two reconciled CROX/IONQ reservations and the earlier CROX weekly reservation remain CANCELLED with zero fills.

Detailed saved holdings, deltas, reservation identities, native read-only planning-check reasons, receipt hashes and source paths are in `ownership-precheck.json`. This subagent made no broker/provider calls, ledger/control/code writes, order actions, supervision renewals or trader starts. Root must review and execute any separately prepared recovery helper.

Prepared `capture_order_status_readonly.py` and `native_reconcile.py` for root review only. Both pass syntax validation and neither was executed by this subagent. They pin the current root supervision lease, exact TWST reservation, terminal session and hash-verified closing-boundary receipt. The native helper preserves session/control/entry/recovery records, acquires native session then cycle locks, backs up SQLite, requires fresh matching-account cancelled zero-fill evidence followed by complete newer unchanged holdings with no working orders or pending shares, and reconciles with zero execution budgets. A WORKING, missing, partial or ambiguous order remains unresolved; no order cancellation is attempted.
