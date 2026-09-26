# September 25 daytime boundary failures

The other failed cycle is immutable decision run `20260925T200003.128416Z`, saved at September 25 **13:00:03.128416 Pacific**. Its exact status is `EXECUTION_WINDOW_CLOSED_AFTER_BROKER_CAPTURE`. Broker capture completed in 3.085 seconds with `CURRENT` status, no retry/error-operation metadata, ready inventory reconciliation, zero selected orders and an empty decision list. The receipt, manifest and decision-file hashes and sizes verify.

The saved receipt does not include the result's separate `error` field. At its recorded timestamp, the pure native execution calendar returns `OUTSIDE_US_EQUITY_ACTIONABLE_SESSION`: the capture crossed the regular-close/POST transition. This is the code-derived reason, distinguished from the directly saved status.

The next recorded cycle, `20260925T200510.012531Z`, completed at **13:05:10.012531 Pacific** with `NO_TRADE`, `CURRENT` broker capture, ready reconciliation, zero selected orders and verified artifacts. That outcome satisfies the session code's branch for clearing the earlier consecutive failure. The final **17:00:01.973703 Pacific** cycle separately crossed the end-of-day cutoff and remained the terminal failure. The final session stays `FINISHED_WITH_ERRORS`; this audit does not change or relabel it.

The bounded receipt inventory contains exactly all **1,639** recorded session calls: 21 `ORDERS_SELECTED`, 1,616 `NO_TRADE`, and these two boundary failures. No additional `UNAVAILABLE` or `SUBMISSION_STOPPED` receipt exists within the inspected session. This inventory is not a review of every daytime order's outcome.

No additional overnight ownership/planning blocker follows from these two cycles. Both had ready reconciliation and returned before decision submission. The separate preflight found no pending ledger reservations or persistent ownership blocks. Continue native preparation; its planning stage must still capture fresh account and ownership evidence. No trader repair, broker request, ledger write, control change or supervision operation was performed.

Exact receipt inventories, verified hashes, source-code hashes and adjacent-cycle evidence are saved in `daytime-failures.json`; the bounded read-only helper is `review_daytime_failures.py`.
