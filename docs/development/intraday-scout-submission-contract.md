# Scout submission foundation: proposed contract

This is a dormant library and an interface proposal for Atlas review, **not an
agreed production wire protocol or evidence that Scout is ready to trade**.
There is no network adapter, broker adapter, service startup, launcher wiring,
account activation or runtime deployment in this change.

Reuse the reviewed forecasting, exact target, scoring and independent sizing
from [PR #78](https://github.com/jeremysecondstate/ducketz/pull/78)
(`53aea68a29ebb136faa4378e89261109100652e3`) and the native compatibility repair
in [PR #80](https://github.com/jeremysecondstate/ducketz/pull/80)
(`a56413a8628e4789aa98852ad607e582267441c0`). No forecasting rebuild or new
sizing policy is proposed. The governing design remains
[sections 5–7 and 12](../15m-trading-system/INTRADAY_15M_SYSTEM_DESIGN.md#5-shared-purchasing-capacity-and-working-quantities)
and [the preserved two-PC architecture](../15m-trading-system/15M-ARCHITECTURE-CHANGE.md#9-preserve-existing-two-pc-and-account-architecture).

**Scout submits and manages its own orders locally.** Atlas hosts the one shared
cash/share reservation pool for Gameplan, Atlas 15m and Scout 15m. This proposal
does not transfer Scout submission to an Atlas execution gateway or delegate
separate cash budgets. SQLite databases stay host-local; telemetry and nightly
file exchange do not supply a live order protocol.

The human relayed that Atlas has no newer admission service. Atlas's response
was not published because approval review blocked it; this statement is not a
durable peer receipt. Correlated setup request
`scout-15m-live-readiness-20261012` remains the coordination identity for reuse,
interface agreement and division of ownership.

Keep live communication small: reservation/fence requests and replies, exact
acceptance identities, fills, cancellation/status changes and confirmed releases.
Preserve detailed local logs and exchange full analytical history separately.
After-the-fact logs cannot prevent two streams committing the same cash/shares.
Do not reuse telemetry payloads as execution commands or add an unrelated framework.

## Current values and injected interfaces

[scout_protocol.py](../../ml/intraday/scout_protocol.py) exposes frozen `Request`
and `Grant` dataclasses, `payload()`, `from_payload(mapping)`, `.sha256`,
`Grant.validate(request)`, `canonical_sha256(mapping)` and `ProtocolError`.
Both payloads have exact keys and fixed `schema_version` =
`scout-intraday-admission-proposal-v1`, `participant_id` = `pc-new`,
`stream` = `scout-15m`, and `kind` = `request` or `grant`.

| Value | Required dataclass fields |
|---|---|
| Request identity | `request_id`, `account_fingerprint`, `session_id`, `symbol` |
| Immutable intention | `side`, `quantity`, `limit_price`, `forecast_id`, `forecast_sha256`, `snapshot_sha256`, `sizing_sha256` |
| Request times | `reference_at`, `issued_at`, `target_close_at`, `valid_until` |
| Grant identity | `grant_id`, `request_id`, `request_sha256`, `account_fingerprint`, `session_id`, `reservation_id` |
| Grant scope | `authority_epoch`, `quantity`, `issued_at`, `expires_at` |

IDs are bounded ASCII; fingerprints are lowercase 64-hex hashes. Forecast ID
equals its forecast hash. Positive integer fields are at most `2**63-1`, with
booleans rejected. Limit price is a positive canonical decimal **string**, at
most 32 characters and eight fractional digits; no exponent, leading integer
zeros or trailing fractional zeros. This is an encoding bound, not new broker
tick policy. Aware timestamps normalize to UTC ISO strings in the attributes.
Request times require `reference < issued < valid_until <= target_close`.
A grant binds the exact request hash/account/session/ID, cannot increase its
quantity, and requires `request.issued_at <= grant.issued_at < expires_at <=
request.valid_until`. There are no grant side/price override fields.

Hashes use sorted compact UTF-8 JSON; this is not RFC 8785 or authentication.
Wire values contain no floats. Unknown/missing fields, malformed offsets and
nonfinite values are rejected. Neither a valid grant nor a payload boolean
proves an authenticated Atlas service or permission to submit.

[scout_client.py](../../ml/intraday/scout_client.py) takes a journal, an injected
`AuthorityTransport`, account/session/symbol bindings, an authority epoch and
an aware clock. The transport has `admit(request_payload) -> grant_payload`
and `deliver(outbox_envelope) -> {event_id, event_sha256}`. It has no supplied
network implementation. `ScoutClient.admit(request)` persists before transport,
then validates current request/grant times, exact binding and the configured
epoch. Lost admission replies leave the same request available for retry.

This is only the **positive admitted-grant subset**. Declines, zero-permitted
responses, allocation-source assignments, renewed grants and release/status
operations are not defined. Actual capacity may still clip desired size to
zero; the existing forecast/intention must remain saved and scoreable. These
missing responses cannot be invented by changing the immutable positive grant.

## Durable local transitions

[scout_journal.py](../../ml/intraday/scout_journal.py) provides `ScoutJournal`:

| Method | Durable effect |
|---|---|
| `prepare(request)` | Create `PREPARED`; repeating the exact request is idempotent. |
| `grant(grant)` | `PREPARED -> GRANTED`; pin grant/reservation IDs and exact hash. |
| `claim_send(request_id)` | `GRANTED -> SENDING`; commit one random send token before returning it. |
| `outcome(request_id, token, broker_order_id)` | Record caller-supplied broker identity as `ACCEPTED`; `None` records `UNKNOWN`. |
| `revoke_unsent(request_id, reason)` | Only `PREPARED`/`GRANTED -> NOT_SENT`; no Atlas capacity release. |
| `pending_events()` / `acknowledge(id, hash)` | Deliver/acknowledge immutable transition evidence; no order/capacity transition. |

An exact late broker receipt can move `SENDING` or `UNKNOWN` to `ACCEPTED`.
`ACCEPTED` cannot be demoted or rebound. `SENDING`/`UNKNOWN` never return to a
sendable state. Journal replay checks schema, immutable records and a hash-linked
outbox. Each transition and its event commit together with SQLite `EXTRA`
synchronization. Outbox envelopes contain token hashes, not send tokens.
Hashes detect corruption; they do not authenticate peers or protect against a
malicious local administrator. Never replace an outstanding journal with a new
file to recover operation; restore and reconcile its original identity/history.

`ScoutClient.submit(request_id, send=..., final_gate=...)` checks the gate,
persists the one send claim, checks the gate again, then invokes the injected
sender with the exact request and grant. Only `True` passes this callback gate;
that return value is not implemented runtime proof. The sender must use the
bound symbol/side/price and granted quantity and verify its exact broker receipt.
The client requires its returned order ID to be a canonical positive integer
string of at most 64 digits; malformed/missing IDs leave `UNKNOWN`, never resend.
The journal's generic ID storage alone does not prove an authentic broker receipt.
Exceptions after claiming conservatively retain uncertainty; process exit can
leave `SENDING`. Even a lost claim return consumes the claim. No path retries
the sender for that durable request. `flush_outbox()` accepts only the exact
event ID/hash acknowledgment. Delivery acknowledgment never releases capacity.

## Required integration before a human Scout start

Atlas owns the service, private authenticated transport binding, durable shared
commitments and complete stream isolation. The service must authenticate both
peers, persist idempotent request results, assign and track allocation sources,
manage authority epochs/fences, count broker/native commitments once, and retain
unknown sends and fills while balances catch up. Proposed sales create no cash.
Define atomic prepare/final-send/receipt/release transitions and partition
behavior: a remote grant alone cannot fence Scout's later local broker POST.
Do not expire/reclaim possibly sent capacity or issue a duplicate sender.

Scout owns its local client/journal integration and broker submission/recovery.
Before **any new same-symbol grant**, resolve the prior stream commitment:
working/partial/unknown orders need exact status, intervening fills and confirmed
terminal cancellation; an unsent reservation needs confirmed Atlas release.
`NOT_SENT` and outbox acknowledgment alone prove neither release nor nonadmission.
Unknown POST without a reliable broker ID retains commitment and never resends
or matches by symbol/price/balance. Broker-supported ambiguous-ID recovery remains
open. Reuse the existing exact-ID Schwab status/cancellation machinery.

The real final gate must verify current manual session and authority fence,
account/producer/symbol ownership, newest eligible live forecast, full ensemble,
observed availability, exact target/version/grid, original sizing evidence,
fresh account/quote data, prior-order reconciliation and strict submission
**before 16:45 Pacific**. The DTO lifetime does not implement these conditions.
No same-forecast repricing timer, Gameplan timeout/expiry, replenishment order,
protected cash share, confidence hurdle or contention priority is selected here.
Temporary failures need durable provider-aware waits; no arbitrary retry ceiling.

Require compatible loaded-code/process/session evidence on both hosts and for
every Atlas account/native writer, including Gameplan and Atlas 15m. New files
on disk and offline passing checks cannot establish that proof. Preserve active
Atlas workers during preparation; coordinate reviewed adoption separately.
Only the human's eventual Scout manual start may invoke the live path.

Offline protocol/journal/client tests use synthetic state and injected callbacks.
They cover schema/scope/lifetime failures, concurrent one-claim behavior,
restart/corruption, dropped admission replies, send ambiguity and exact outbox
acknowledgments. Future joint tests must cover all three streams sharing capacity,
fence expiry and partitions at every send boundary, exact partial fills/cancel
races, confirmed releases and old-loaded-writer rejection. No test here proves
a deployed service, real authentication, broker readiness or live permission.
