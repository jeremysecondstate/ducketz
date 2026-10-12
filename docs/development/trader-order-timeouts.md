# Bounded Gameplan order retries

Managed Gameplan catch-up orders use two bounded attempts, for both buys and
sales. The existing current-quote limit-price policy and all account, activation,
inventory, cash, quote-freshness and submission checks apply to each attempt.

1. Once the original submitted order has been working for five minutes, request
   cancellation of its unfilled remainder on the next executable worker cycle.
2. Keep its shares or cash reserved until exact broker evidence confirms a
   terminal cancellation. Preserve any fills that occurred before cancellation.
3. If the accepted plan still calls for that direction and quantity, permit one
   refreshed order, capped by the original unfilled remainder and current
   execution limits. Its price comes from the normal current-quote builder.
4. That submitted replacement gets its own five-minute interval. Request
   cancellation if it remains unfilled. After terminal confirmation, permanently
   abandon its remaining quantity for that original intention. A broker-confirmed
   cancellation or expiry of the replacement also ends its retry allowance.

The intervals start from immutable submission evidence, not forecast time or
the most recent broker poll. Legacy orders can recover their original submission
time from the existing immutable audit. Missing timing evidence fails closed.
Polling and broker response time can extend the wall-clock duration; five minutes
is a cancellation threshold, not a fill guarantee or an exact cancellation time.

## Durable accounting

The horizon ledger retains submission times, timeout cancellation reservations
and explicit original-to-retry links. The link is recorded before submission.
Restarting the process, refreshing quotes or changing a derived prediction hash
does not create another retry allowance. Separate original orders each retain
their own bounded retry; an instruction split by existing sizing constraints
does not accidentally consume its allowance just because it used several orders.

Cancellation requests never manufacture fills or release reservations. Unknown
submission/cancellation outcomes remain reserved for reconciliation. A confirmed
broker rejection keeps its existing review requirement. A proven local stop
before any broker submission does not consume the actual refreshed attempt.

The retry's intended net quantity is frozen after newly due instructions have
been accounted for. If execution limits permit only a smaller refreshed order,
the unsent portion of that intended quantity is abandoned as well. This avoids a later cash or
inventory change manufacturing a third attempt. Abandoned quantities are
separate from real fills and reservations. Catch-up subtracts them from the
accepted plan's remaining intention without changing holdings or recording a
fictional fill. New scheduled instructions add their own quantities; they do not
resurrect the previously abandoned amount.

The saved decision metadata records `catchup.abandoned_intentions`. A clean
no-order cycle with abandoned quantities reports
`NO_ORDERS_SUBMITTED_RETRY_EXHAUSTED`; genuine errors retain precedence. Both
native catch-up decisions and combined-account authority reconstruct the same
remaining intention from the same durable reservation evidence.

## Scope and operation

The rule covers Gameplan direction-ledger/accepted-plan catch-up reservations.
Legacy learned/fixed sizing and legacy forecast-only cancellation rules retain
their existing behavior. Manual and unrelated account orders are not canceled.

Verification uses synthetic broker responses and temporary databases. Source
installation does not change manual trader start/stop controls or reload a
running worker. A subsequent manually started worker loads the policy and can
manage existing qualifying orders using their saved submission evidence.
