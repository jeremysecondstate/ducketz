# Stock direction and horizon autonomy — October 19, 2026

Jeremy's direct instruction applies to Atlas and Scout and every symbol in the
combined Gameplan and independent 15-minute traders. P(up) of 50.01% or higher
requests BUY; 49.99% or lower requests SELL. Values strictly between those
boundaries request HOLD. Additional confidence thresholds and confidence sizing
multipliers must not turn a qualifying direction into a hold. Actual cash,
available shares, live quotes, existing order reservations and order sizing
ceilings still determine executable quantity.

For action sessions from October 19 onward, Gameplan 1h and 4h bearish signals
may sell available shares from any Gameplan horizon, including shorter horizons.
They do not need an empty own allocation and do not use fallback slot quotas,
half-day caps or another horizon's bullish permission. Reservations retain the
exact owner slices so fills reduce the actual source allocations once. This
does not introduce a dependency on either independent 15-minute trader.

The existing 15-minute autonomy continues. The 1d and 1w ownership and bounded
fallback rules continue unchanged. Historical plans retain their original
direction policy and evidence. A new informational projection can record a
policy revision while retaining the original probabilities, observation clock
and immutable source artifacts. Planning estimates never supply execution
prices: orders use current broker bid, ask or midpoint and actual capacity.

Gameplan no longer presents Planning price or Execution mid columns or saved
price estimates in row details. Recorded actual execution remains separate.
This source change does not start or restart any trader; manual controls remain
in force.
