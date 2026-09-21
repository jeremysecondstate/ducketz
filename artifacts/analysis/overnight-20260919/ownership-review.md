# Read-only ownership diagnosis

The September 21 trade-plan tail correctly failed closed with `OWNERSHIP_SNAPSHOT_UNAVAILABLE`. Its fresh account snapshot is observed, account matches, and broker reports zero working orders. The ledger still reserves 52 CROX weekly BUY shares at $120.51 for exact broker order `1007973368571`; no other reservation is open.

Last order evidence at 2026-09-18 12:00:05.766412 UTC says WORKING, zero fills, 52 remaining. At 12:00:07.891114 UTC the native ledger reserved the sole cancellation attempt. The 12:00:08 decision receipt reports `ENTRY_CANCELLATION_AWAITING_RECONCILIATION`. The next cycle at 12:00:14 failed order-history capture; the session eventually ended `FINISHED_WITH_ERRORS`, 1,789 failed cycles, last error `OrderHistoryError: INVALID_BROKER_NUMERIC_EVIDENCE`. No terminal order confirmation is saved in the native ledger. The nightly snapshot has CROX 0 and otherwise matching owned quantities.

This establishes a stale saved reservation following a cancellation request, not proof of broker terminal status. Fresh exact-order evidence is required. The normalizer validates top-level quantity, leg quantity, filledQuantity and remainingQuantity before classifying CANCELED/CANCELLED/EXPIRED; a missing/invalid field can explain this error but its exact cause cannot be established from saved local evidence alone.

Recovery must preserve the reservation until matching-account, exact-order terminal status and complete cumulative fills are verified, followed by a newer coherent account snapshot. Any approved native reconciliation must occur independently of trade planning. Do not clear the row manually, infer cancellation from absence in working orders, replay the cancellation, start a trader, assign existing holdings, or bypass ownership safety. After reconciliation, resume only the failed trade-planning/actuals tail with the same pinned publication and original deadline.

Read-only SQLite queries and local saved artifacts only; no broker calls, process control, production edits, trading-code edits or ledger writes were performed. Evidence paths and hashes are recorded in `ownership-review.json`.
