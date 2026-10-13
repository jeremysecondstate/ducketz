# Retained shared-admission implementation

The Atlas-hosted admission service is a retained implementation, not the
operating contract for the independent traders. Scout's default manual launcher
uses `broker-local` and the [local submission contract](intraday-scout-submission-contract.md).
No service installation, Atlas writer proof, private route, TLS enrollment,
shared reservation, service acknowledgment or remote readiness round trip is a
prerequisite for Scout's trader or nightly Gameplan delivery.

## Current standing operations

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

## Preservation boundary

The retained `admission_service.py`, `admission_transport.py`, account/native
ledger adapters and their tests describe the earlier shared-admission protocol.
Do not start that service or migrate a private ledger merely because its source
is installed. Existing `atlas-service` code is an explicit legacy selection;
there is no automatic fallback to it and no request to configure it.

Original remote-mode orders, if any, retain their own exact request/order IDs
and saved evidence. Never reinterpret an old remote grant as a new local permit.
Own-order recovery prevents duplicate effects; it does not create a requirement
to inspect another trader's orders, runtime or readiness. New independent Scout
orders and their audit trail stay in Scout's local books.

The previous distributed-service specification is retained in
[the October 13 source checkpoint](https://github.com/jeremysecondstate/ducketz/tree/cfac28bf0c7fb144187b62d5136810c5a4dec73f/docs/development).
Its source, tests, private originals, request IDs, grants, certificates and
receipts are historical evidence. Preserve them; do not manufacture enrollment,
relocate databases, relabel an old order or treat that evidence as a requirement
for current independent execution.
