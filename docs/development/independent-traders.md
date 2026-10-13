# Independent trading systems

Jeremy's October 13, 2026 direction defines three independent trading systems:

Atlas runs two traders: the combined Gameplan trader and Atlas's own 15-minute
trader. Scout runs one trader: Scout's own 15-minute trader. Each PC produces
an 11-symbol Gameplan; the single synthesis produces one 22-symbol Gameplan.

- The Gameplan trader uses the single combined Atlas–Scout Gameplan.
- Atlas's 15-minute trader makes and executes its own forecasts.
- Scout's 15-minute trader makes and executes its own forecasts.

Only the two Gameplan research packages are synthesized. A 15-minute trader
does not contribute to that synthesis. No trader requires another trader to
be running, healthy, compatible, reconciled, verified, or accepted. Neither
15-minute trader combines its forecasts, orders, or operating state with the
other one. Each trader records its own activity and uses its own execution
state and manual session.

Shared broker connectivity and reusable source code do not create a runtime
dependency between these systems. Each trader uses actual broker cash,
positions, and order responses when placing its own orders. Its own order
IDs and fills remain its own records; another trader's internal ledger or
health report is not an input or a start condition.

The transition to separate execution state preserves the original mixed
ledgers and their historical fills. New independent state must not invent
fills or rewrite prior activity. Any unresolved order belonging to a trader
must retain its real identity across that trader's transition.

Manual launch remains explicit. Preparing source, Gameplan publications, and
local bindings does not start a trader or place an order.

## Standing authorization for every night

Jeremy has pre-authorized this operation permanently. No verification or
approval is required from Jeremy, the peer machine, or another trader. This is
the normal nightly operation, not a one-time recovery or an exception process.
Do not create verification stages, approval requests, readiness prerequisites,
compatibility conditions, source-transition ceremonies, or reciprocal acceptance
round trips that hold up these operations.

Every night, produce the two Gameplan research packages, synthesize those
Gameplans once, publish the combined Gameplan and Stats, and prepare each
machine's independent 15-minute trader for manual start. Atlas can complete the
synthesis directly from the two packages. Sending the completed display to
Scout does not delay Atlas. A trader's activity, availability, or internal state
does not delay any other trader or Gameplan production.

Handle real operating exceptions directly under this standing authorization.
Keep useful progress and resume the unfinished work. Record the actual error
and result; do not invent successful data, repeat acknowledgment loops, or ask
Jeremy to supervise routine overnight recovery. Preparation does not turn on
trading: Jeremy still starts each trader manually.

This direction supersedes older documentation or task prose requiring
cross-trader ownership projection, loaded compatibility attestations, shared
trading ledgers, or reconciliation between Gameplan and 15-minute traders.
