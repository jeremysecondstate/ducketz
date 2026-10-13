# Scout independent local submission contract

The current execution path is `ScoutLocalAccountExecutor` in
`ml/intraday/scout_local_execution.py`, selected by `broker-local`. It owns real
local broker submission and exact-ID order management after the human's manual
start. It does not contact Atlas or load Gameplan's account configuration.

## Standing execution and nightly boundary

Jeremy's October 13, 2026 instruction is standing preauthorization for every
night's existing preparation, Gameplan synthesis and delivery. Continue those
operations automatically without requesting human or peer confirmation. The
nightly handoff is Atlas Gameplan + Scout Gameplan -> Scout synthesizes -> Scout
sends the result to Atlas. Sending that completed result finishes the handoff;
there is no third account snapshot, Atlas acceptance, cross-trader verification,
reconciliation or readiness ceremony before completion or the next night's run.

Gameplan, Atlas 15m and Scout 15m execute independently and keep their own logs.
Each trader obtains actual capacity from its own existing broker connection and
manages only its own orders. No trader waits for another trader's state, service,
locks, approval or reconciliation. Jeremy's manual first trader start remains a
manual action; it is not an invitation to ask for approval again. The existing
exact-byte source publication and private-data handling rules remain engineering
and privacy procedures, never additional nightly operating prerequisites.

## Local identity and actual capacity

At manual startup Scout uses its existing broker's stable account fingerprint,
its own profile/watchlist and its own private books. Later local effects keep
that account and manual-session identity. A missing Gameplan configuration,
private Atlas endpoint, client certificate, service proof or peer receipt is
not a setup requirement. `intraday/scout/execution.lock` serializes Scout's own
effects; the Gameplan/hourly lock and other traders' process state are not used.

Every eligible forecast keeps its original full sizing intention. A fresh
broker capture provides available cash already net of pending BUYs and actual
held shares less pending SELLs. Clip to the affordable or available whole-share
quantity, including zero. Do not subtract working BUY cash twice, establish
stream budgets, invent horizon allocation ownership, or infer sale proceeds.
Other traders' orders matter only through the broker's observed account capacity.
Independent account reads are not atomic cross-PC reservations; broker acceptance
or rejection is the actual order result.

## Durable local effects and own-order recovery

The existing Request/Grant structures are immutable local DTOs, not evidence of
Atlas authorization. Local IDs use `scout-local:`, `local-permit:` and
`local-order:` prefixes. Before an effect, a private `local-account-permit`
receipt binds the exact request, full desired size, adjusted capacity, account
snapshot, broker identity and manual session. The send callback rereads the
saved receipt and retains the one-send journal fence.

The existing prepared Schwab adapter checks the exact account/order response,
quote age, fixed forecast target, real market window and strict pre-16:45 Pacific
submission time. A timeout never proves nonacceptance. Unknown sends remain
recorded without another POST or a guessed broker ID. Accepted orders recover
only by their exact IDs, with cumulative fills and original timestamps.

A newer same-symbol Scout intention is saved while its own prior order finishes
or is cancelled. Cancellation acknowledgment is not terminal status. Record
intervening fills and wait for exact terminal evidence before replacing that
Scout order. Do not cancel or manage another trader's orders. Local audit events
do not need an Atlas acknowledgment. Missing cost basis or realized P&L stays
unknown. The actual broker quantities and local lifecycle history remain intact.

See [the manual command and local logs](scout-local-15m-manual-start.md).

## Retained distributed implementation

The previous distributed-service specification is retained in
[the October 13 source checkpoint](https://github.com/jeremysecondstate/ducketz/tree/cfac28bf0c7fb144187b62d5136810c5a4dec73f/docs/development).
Its source, tests, private originals, request IDs, grants, certificates and
receipts are historical evidence. Preserve them; do not manufacture enrollment,
relocate databases, relabel an old order or treat that evidence as a requirement
for current independent execution.
