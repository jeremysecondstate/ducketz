# Confirmed broker rejection during Gameplan catch-up

The ordinary trader nets each accepted Gameplan's due intentions against actual
fills and open reservations. A confirmed broker rejection has no filled or open
quantity, but it is an unresolved execution failure: it must not manufacture a
new prediction identity and repeatedly submit the same economic intention.

`catchup_signals` holds the affected accepted-plan, symbol, horizon and direction
when its durable ledger contains a terminal `REJECTED` reservation with an exact
broker order identity. Reopening the process or changing the derived catch-up
prediction hash does not remove that evidence. Local safety stops before a POST
have no broker order identity and are distinguished from broker rejections.
Confirmed partial fills and outstanding quantities keep their usual accounting;
confirmed cancelled residuals keep their existing behavior. Other families and
the opposite direction can continue through normal reconciliation and controls.

The decision publication records `prediction_handoff.catchup.blocked_intentions`
with the original reservation reference and observed time. The cycle returns
`CATCHUP_BROKER_REJECTION_REVIEW_REQUIRED`, or
`ORDERS_SUBMITTED_WITH_REJECTION_REVIEW_REQUIRED` if other eligible intentions
were submitted. Its error marks the supervised session degraded while allowing
continued reconciliation of existing orders. There is no second executor and no
automatic cancellation, trader restart, control change or test submission.

Review the saved private ledger and original execution events before proposing a
repair. Newly observed broker `statusDescription` is retained in private order
evidence. Older evidence without that field remains unchanged; missing historical
detail is unknown, not a guessed rejection reason. A provider response returned
with an order location is a submission acknowledgment, not proof of a fill or
proof that the broker kept the order working.

This guard has no expiry takeover or automatic reset for the same frozen plan.
Do not delete reservations, rewrite terminal statuses, regenerate a completed
plan, or change a plan hash to bypass it. Any later corrective retry requires a
separately reviewed evidence-bound route; this fix does not authorize one. A
genuinely new accepted action-date plan retains its ordinary independent
identity. Jeremy continues to control manual trader start and stop.

Before selecting a Gameplan order for submission, the normal trader checks the
complete current account-wide working-order identities captured in that cycle.
An opposite-side equity order at the same decimal limit price defers only the
conflicting intention. This includes manual orders, other horizons and earlier
eligible decisions in the same ordered batch. Existing decision ordering keeps
sales ahead of purchases. Options are not treated as the same equity, and other
strategies retain their existing behavior. Missing or incomplete equity order
evidence does not mean that no orders exist.

The deferred decision retains its prediction and decision identities and records
`OPPOSING_SAME_PRICE_WORKING_ORDER` with no selected quantity or order payload.
`prediction_handoff.self_trade_prevention` binds the current observation and
conflicting private order or batch-decision identities. No native or account
execution reservation is consumed for that intention. The cycle reports
`SELF_TRADE_CONFLICT_DEFERRED`, or `ORDERS_SUBMITTED_WITH_SELF_TRADE_CONFLICT` when
other intentions were submitted. A later normal capture can clear the deferral
once the conflicting order is no longer working; a confirmed broker rejection
still requires the separate review described above.

The check uses the existing bounded-age broker snapshot and submission gates.
It introduces no extra broker preflight, cancellation, repricing, netting between
owners or manual-control change. Existing fills and working orders continue
through normal reconciliation; the broker remains authoritative if account
orders change after the captured observation.
