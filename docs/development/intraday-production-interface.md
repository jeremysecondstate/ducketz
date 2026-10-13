# Independent trader interfaces and retained Atlas host

The current Scout production entry point is the existing manual launcher in
`ml.intraday.live_session` with `--scout-admission broker-local`. The executor
connects directly to its existing local broker after the manual start. It has
no Atlas endpoint, certificate exchange, Gameplan configuration, loaded-writer
proof or service-response requirement.

## Standing interface boundary

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

## Local production evidence

The [local submission contract](intraday-scout-submission-contract.md) describes
the real prepared broker POST, exact accepted-ID status/cancellation, immutable
local permits and one-send fencing. The account fingerprint comes from this
trader's existing broker at manual startup. Quotes, actual account capacity,
forecast/sizing evidence and its own controls belong to that local execution.
No readiness payload from another trader authorizes or blocks those effects.

The trader writes its own status, events, forecasts, intentions, fills and
cancellation history. Optional monitoring reads saved logs and reports actual
observations; monitoring installation, a peer receipt or a UI refresh is not an
execution prerequisite. Never claim live acceptance from an offline fixture.

## Legacy host and enrollment API

The earlier `/v1/readiness`, `/v1/accounting`, admission and private
`admission_offer`/`admission_csr`/`admission_cert` interfaces remain documented
by their retained code and versioned history. They are not steps in the current
manual-start or recurring nightly procedure. Preserve any original keys,
certificates, packet selections and account data privately. Do not weaken TLS,
copy databases or generate placeholder bindings to make that old path appear
ready. There is no reason to collect a LAN/VPN route for independent execution.

The previous distributed-service specification is retained in
[the October 13 source checkpoint](https://github.com/jeremysecondstate/ducketz/tree/cfac28bf0c7fb144187b62d5136810c5a4dec73f/docs/development).
Its source, tests, private originals, request IDs, grants, certificates and
receipts are historical evidence. Preserve them; do not manufacture enrollment,
relocate databases, relabel an old order or treat that evidence as a requirement
for current independent execution.
