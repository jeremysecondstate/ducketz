# Scout local submission and shared admission contract

Scout's integration composes the existing forecasting, sizing, scoring and
recovery implementation with an authenticated admission client, durable private
journals and a participant-aware manual launcher. Source completion is distinct
from installed bytes, a deployed Atlas service, compatible loaded writers and
manual-start readiness. The [manual-start runbook](scout-15m-manual-start.md)
defines the acceptance evidence and human-controlled start/stop procedure.

The reviewed lineage is [PR #78](https://github.com/jeremysecondstate/ducketz/pull/78)
(`53aea68a29ebb136faa4378e89261109100652e3`), the compatibility repair in
[PR #80](https://github.com/jeremysecondstate/ducketz/pull/80)
(`a56413a8628e4789aa98852ad607e582267441c0`), and the foundation in
[PR #81](https://github.com/jeremysecondstate/ducketz/pull/81)
(`31d6551329b8aeac0eb382db1e352a87541853fa`). The governing design remains
[the 15m system design](../15m-trading-system/INTRADAY_15M_SYSTEM_DESIGN.md)
and [the preserved two-PC architecture](../15m-trading-system/15M-ARCHITECTURE-CHANGE.md).
Coordination reuses request `scout-15m-live-readiness-20261012`.

## Ownership and shared capacity

Scout submits and manages its own orders locally. Atlas owns the serialized
authenticated admission boundary and its existing account authority/native
ledgers. Gameplan, Atlas 15m and Scout 15m share one actual cash/share pool.
Quantities may shrink, including to zero, and eligible same-symbol shares may
come from different horizons under existing ownership/reservation rules. No
stream budget, protected cash allocation or new contention priority is added.

Scout owns its local adapter, journal collection, orchestration and private
transport binding. Atlas's databases remain Atlas-local; Scout neither copies
them nor creates an Atlas coordinator identity. Participant authentication does
not authorize Scout to manage Atlas or Gameplan orders. Immediate communication
carries reservation, send and exact order-lifecycle facts. Detailed analytical
history and accounting exchange remain separate from live admission.

## Stable request/grant values

[scout_protocol.py](../../ml/intraday/scout_protocol.py) supplies immutable
`Request` and `Grant` values, exact payload parsing and canonical SHA256 hashes.
Their encoding retains `schema_version = scout-intraday-admission-proposal-v1`,
`participant_id = pc-new`, `stream = scout-15m` and `kind = request` or `grant`.
The historical `proposal-v1` discriminator is retained for byte compatibility;
the enclosing authenticated wire protocol is separately versioned.

| Value | Required dataclass fields |
|---|---|
| Request identity | `request_id`, `account_fingerprint`, `session_id`, `symbol` |
| Immutable intention | `side`, `quantity`, `limit_price`, `forecast_id`, `forecast_sha256`, `snapshot_sha256`, `sizing_sha256` |
| Request times | `reference_at`, `issued_at`, `target_close_at`, `valid_until` |
| Grant identity | `grant_id`, `request_id`, `request_sha256`, `account_fingerprint`, `session_id`, `reservation_id` |
| Grant scope | `authority_epoch`, `quantity`, `issued_at`, `expires_at` |

IDs are bounded ASCII and fingerprints lowercase 64-hex strings. Forecast ID
equals its forecast hash. Positive integer fields reject booleans and exceed
neither `2**63-1` nor the original desired quantity. Limit price is a positive
canonical decimal string with bounded length/precision, not a float. Aware
timestamps normalize to UTC. Request ordering is
`reference < issued < valid_until <= target_close`; grant expiry cannot exceed
request validity. A grant binds the exact request/account/session and supplies
no side or price override. Sorted compact UTF-8 JSON hashes detect changed bytes;
they are not authentication or RFC 8785 canonicalization.

The injected [scout_client.py](../../ml/intraday/scout_client.py) foundation
supports positive admitted grants and one-use submission. Its generic
`admit`/`deliver` interface is not the complete production wire protocol.
Zero capacity and declines remain service decisions, never invalid zero-sized
positive grants. Their original forecasts and intentions stay scoreable.

## Authenticated authority client and send boundary

[scout_authority.py](../../ml/intraday/scout_authority.py) implements Scout's
client for `intraday-shared-admission-v1`, following the
[Atlas service contract](intraday-shared-admission-service.md). HTTPS requires
the configured CA, server hostname validation and Scout client certificate/key.
The private binding pins account, participant, stream, symbols and authority
identity. Foreign streams, changed duplicate requests and incompatible protocol
versions fail closed. A payload's participant name cannot replace transport
authentication.

Scout persists its immutable request before reservation. A positive reservation
is followed by a durable local send claim and Atlas's durable authenticated send
authorization before Scout's prepared broker POST. Only the successful current
`SEND_AUTHORIZED_ONCE` reply authorizes that invocation. Retrying a claim,
recovering a lost reply or reading status cannot supply another send permission.
A lost authorization reply retains the commitment and requires reconciliation.
The service's pre-POST `SUBMITTING` state and native `UNKNOWN` projection are
ordinary in-flight evidence; neither alone proves a stranded order.

The binding lives privately at `DATASTORE/intraday/scout/authority-binding.json`.
Validated authority replies are saved under `DATASTORE/intraday/scout/receipts`.
Saved content hashes detect alteration; authenticated transport and exact
request/response validation establish the reply's origin and scope. Endpoints,
private keys, account fingerprints and order data stay out of shared source and
public coordination messages.

Capability discovery establishes authenticated identity/protocol support. A
separate readiness check must establish actual compatible loaded Atlas account
writers or verified absence. The client remains fail-closed while the exact
readiness route/schema is unpublished or unavailable. A constant successful
callback is only a test fixture. New files on disk do not prove loaded code.

## Durable local submission and original-session books

[scout_journal.py](../../ml/intraday/scout_journal.py) commits each submission
transition with its immutable event using SQLite `EXTRA` synchronization.

| Operation | Durable effect |
|---|---|
| `prepare(request)` | Create `PREPARED`; an exact duplicate is idempotent. |
| `grant(grant)` | `PREPARED -> GRANTED`; pin exact grant/reservation identity. |
| `claim_send(request_id)` | `GRANTED -> SENDING`; consume one send token before returning. |
| `outcome(request_id, token, broker_order_id)` | Persist exact acceptance as `ACCEPTED`; absent reliable ID becomes `UNKNOWN`. |
| `revoke_unsent(request_id, reason)` | Only `PREPARED`/`GRANTED -> NOT_SENT`; no authority capacity release. |
| Event acknowledgment | Clear exact event-ID/hash delivery work; no order or capacity transition. |

An exact late broker receipt may move `SENDING`/`UNKNOWN` to `ACCEPTED`.
Acceptance cannot be demoted or rebound. Neither `SENDING` nor `UNKNOWN` becomes
sendable again, including after a crash or lost claim return. Generic journal ID
storage alone is not a broker receipt. The prepared adapter validates the exact
Schwab HTTPS Location, prepared account and canonical order ID. Missing or
malformed acceptance retains uncertainty; it never triggers a repeat POST or a
symbol/price/balance-based guess.

[scout_books.py](../../ml/intraday/scout_books.py) inventories original journals
under `DATASTORE/intraday/scout`. `inventory.json` pins each session's account,
participant, stream, filename and immutable journal ID. A later manual session
audits every original before registering its own journal. Missing, replaced,
corrupted, unregistered or residual journal files block startup for repair.
Registered journals open with `create=False` and their expected original ID;
starting a new session cannot erase an earlier one-use claim.

The local lifecycle retains exact accepted-order observations, complete
cumulative fills and cancellation intent/attempt evidence. Fill totals cannot
regress or change identity, and terminal evidence cannot be demoted. A recognized
legacy journal requires explicit migration preserving original claims and
outbox bytes. Unchanged observations advance an indexed latest receipt without
duplicating full replay events. The local reservation projection serves the
existing cancellation interface; it is not a second allocation ledger.

## Local orchestration and cancellation

[scout_execution.py](../../ml/intraday/scout_execution.py) composes existing
sizing, capacity validation, the prepared broker adapter and exact-ID recovery.
It keeps original sizing and dispositions in the existing forecast store.
Before replacement for a symbol, it resolves prior commitments across the
original Scout session journals. Working/partial orders require exact status,
intervening fills and terminal cancellation evidence. Unsent reservations
require confirmed authority release. A local `NOT_SENT` event or message ACK
does not prove either nonadmission or released capacity.

Cancellation persists intent before DELETE and records the attempt separately
from broker status. An explicit proved-before-DELETE retry permission is consumed
durably before reentering transport. Its send-claim event is an in-flight attempt,
not an acknowledgment or terminal result. Lost/ambiguous effects retain the
commitment. A DELETE acknowledgment never releases capacity; exact cumulative
terminal evidence must reach the authority before replacement can use capacity.
Recovery continues to honor original journal/session/order identity after reload.

Only `ACCEPTED`/`UNKNOWN` outbox events represent actionable lifecycle deliveries
in the current wrapper. `PREPARED`, `GRANTED`, `SENDING` and `NOT_SENT` are local
audit facts; reserve/claim/release facts travel through dedicated authenticated
RPCs and receipts. Raw unacknowledged event counts are not a wire-backlog count.
An event ACK clears delivery work only. An authenticated `NOT_SUBMITTED` release
can clear shared capacity while the local `UNKNOWN` fence remains permanently
non-sendable. Local terminal evidence alone does not prove authority release.

For an expired original request that never obtained admission, Scout validates
and saves Atlas's exact `intraday-negative-admission-v1` proof before changing
its grantless `PREPARED` record. The proof binds the original request, session,
account and authenticated server, and certifies absence from both canonical
books and retained send-claim history. It neither releases an existing
reservation nor rearms a local send claim. Missing, contradictory or future
proof fails closed; a local `NOT_SENT` label cannot substitute for it.

Each startup and guarded operation verifies Scout-local account-writer absence
while holding the existing `stock-trader-hourly.lock` through the protected
operation. Gameplan remains Atlas-owned. Atlas independently checks its own
writers under its authority lock. A point-in-time unlocked prestart absence
check cannot fence a later scheduled writer; runtime locks and fresh checks do.

## Runtime gates and preparation evidence

[scout_runtime_gate.py](../../ml/intraday/scout_runtime_gate.py) checks exact saved
Scout forecasts, original session, immutable snapshot/target, current manual
controls, quote freshness, market window and strict submission before 16:45
Pacific. Local SQLite checks use `mode=ro`, `query_only` and zero lock wait.
A newer full current-session forecast supersedes an older one even when it
arrives during preceding account IO. The executor also verifies original sizing,
rounded quote limit, authenticated send receipt, compatible writers and prior
order clearance. Account verification finishes before the final local gate.
Reconciliation and exact cancellation may continue after cutoff or target expiry.

The shared [manual launcher](../../ml/intraday/live_session.py) preserves Atlas
defaults and explicitly selects Scout's executor from the local profile. Scout
requires authenticated, protocol-compatible readiness before provider
construction, history loading, model work and `RUNNING` status. Each session
records PID/birth time, participant and actual evidence paths. No permissive
fallback replaces failed readiness. Ctrl+C withdraws manual authority and waits
for active computation; it does not automatically cancel orders or release books.

[scout_prestart.py](../../ml/intraday/scout_prestart.py) checks the pinned release,
Scout profile, final exact installed manifest, dependency metadata, private
binding, local process absence and original journals. Book inspection uses raw
SQLite `mode=ro`/`query_only`; it never constructs production stores or
broker/provider clients. Optional connection checks call only authenticated
capabilities/readiness and save dedicated private prestart receipts. No trading
commitment is created.

Offline tests must cover the exact final dependency closure: shared capacity
contention/reduction, participant isolation, mutated duplicates, one-use claims,
crash/lost replies, cumulative fills, cancellation races, release evidence,
controls, freshness and incompatible writers. Historical test counts do not
cover later bytes. Publication, installation, non-ordering service connectivity
and post-start session evidence must remain separate completion facts. Only the
human's eventual manual start invokes the real-order session.
