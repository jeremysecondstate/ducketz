# Shared admission service for Scout

This change supplies the shared book and authenticated transport components for
Scout-local submission. It extends the existing Atlas implementation; it does
not replace forecasting, sizing, scoring, UI, exact fill accounting or native
cancellation recovery. The reviewed base is PR #81,
`31d6551329b8aeac0eb382db1e352a87541853fa`, which includes #80 and the #78 lineage.
PR #77 is a separate sibling and is not included.

The components are dormant. Publication, installation, loaded worker
compatibility, private service setup and manual live readiness are separate
facts. This source change does not start a server, change a schedule, remove
the existing Atlas-only launcher guard or authorize a Scout broker operation.

## Ownership and books

Atlas owns `admission_service.py`, `admission_transport.py`, the account
authority, native ledger and shared stream compatibility. Scout owns its
local broker adapter, submission journal integration and participant-aware
session wiring. Coordinate changes to those Scout-owned files before editing
them. The PR #81 request, grant, journal and client foundation is reused as a
dependency; it is not yet a complete client for the send-claim exchange below.

The authority uses one account pool with pending commitments from all streams.
There are no protected cash allocations or per-stream budgets. Admission uses
the existing capacity clipper and may grant fewer shares than requested.
Eligible shares for a symbol can come from different Gameplan horizons using
the existing native sale slices. Participant symbol ownership remains the
explicit disjoint registry already required by `AuthorityBinding`; this does
not authorize one PC to manage the other PC's orders.

Scout keeps its own durable submission/recovery journal and detailed local
books. Atlas mirrors reservations and exact broker evidence into its existing
canonical account and native books. No SQLite file is copied or network-shared.
All service book operations serialize under the existing common stock-trader
lock, with a fresh coordinator lease supplied for each operation. Gameplan
order cancellation, timeout/retry and scheduled exit actions reject intraday
streams; shared accounting can still reconcile their exact broker evidence.

## Host API and remaining production bindings

`AdmissionService(datastore=..., binding=..., server_identity=...,
compatibility=..., context_factory=..., refresh=..., clock=...)` accepts three
reviewed host callbacks. They are explicit integration points, not supplied
live adapters:

* `compatibility()` must return a mapping with `ready`, `atlas_ready` and
  `gameplan_ready` all exactly `True` only after checking actual loaded writers
  or independently proving their absence. A constant successful callback is
  a test fixture, not a production configuration.
* `context_factory()` is a context manager yielding
  `BookOperation(authority, ledger, lease)`. It must open the Atlas-local
  `state/account-gameplan/authority.sqlite3` and
  `state/independent-stock-trader/holdings.sqlite3` under the supplied DATASTORE,
  using the existing private account binding and a current coordinator lease.
  The service already holds `locks/stock-trader-hourly.lock`; do not recursively
  acquire that lock in the callback. Release/close the lease and handles when
  the context exits.
* `refresh(operation)` must perform the existing reviewed fresh account/native
  capture and reconciliation, then publish the same snapshot to both books.
  Return `BookSnapshot(snapshot_id, observed_at, pending_sell_shares,
  broker_pending_sell_orders)`. Do not fabricate balances or omit external
  orders. The existing Atlas capture now distinguishes a strict, paired,
  unclaimed Scout reservation from an uncertain broker submission.

These production callbacks, their private bindings and a host entry point
still need integration and verification. They must reuse existing capture and
authority code. This publication does not provide a configured endpoint.

Loaded-code support includes the expanded Gameplan capability
`gameplan-explicit-cross-pc-stream-isolation-v2` and
`expected_intraday_executor_fingerprints(repository)` /
`actual_intraday_executor_fingerprints(executor)`. The latter fingerprints
bound methods and their retained shared dependencies, including the actual
contextmanager body. Atlas executor heartbeat emission and verification tied
to process identity/birth, freshness and session ownership still need wiring.
An on-disk hash or a new helper import cannot prove that an older running
worker loaded this change. Do not admit Scout while incompatible writers run.

## Authenticated wire contract

The transport protocol is `intraday-shared-admission-v1`. It uses HTTPS with
mutual TLS, TLS 1.2 or later, server hostname verification and an explicit local
SHA-256 DER client-certificate-to-participant map. The authenticated certificate
selects `pc-new`; a request body cannot select an identity. Certificate keys,
CA material, account fingerprints, endpoints and bindings stay private.

`server_tls_context(...)`, `client_tls_context(...)`,
`CertificateParticipants(...)`, `AdmissionHTTPSServer(...)` and
`AdmissionHTTPSClient(...).call(method, path, payload)` provide the transport.
Server construction binds only when explicitly invoked; imports do not listen.
The client does not follow redirects or automatically retry POSTs. JSON is
bounded to one MiB and rejects duplicate keys and nonfinite numbers. Successful
replies are service dictionaries; transport errors are sanitized.

Every POST must carry exactly one `X-Admission-Protocol` header equal to
`intraday-shared-admission-v1`. Missing, duplicate or different versions are
rejected before reading the body or invoking the service. Mutual TLS identity
verification is independently required. The read-only capabilities GET remains
available without this request header; it still authenticates the participant.

| Operation | Route | Request body |
| --- | --- | --- |
| Read-only capabilities | `GET /v1/capabilities` | None |
| Reserve or decline | `POST /v1/reserve` | Exact PR #81 `Request.payload()` |
| Claim one send | `POST /v1/authorize-send` | Common fields plus `claim_id` |
| Recover book status | `POST /v1/status` | Common fields only |
| Submit exact evidence | `POST /v1/evidence` | Common fields plus event fields below |
| Record local cancel intent | `POST /v1/cancel-intent` | Common fields plus `event_id`, `claim_id`, `broker_order_id`, `reason` |
| Revoke before any send claim | `POST /v1/release-unsent` | Common fields plus `event_id`, `proof_sha256` |

Common fields are exactly `request_id`, `request_sha256`, `session_id`. Unknown
or missing fields are rejected. Evidence event fields are `event_id`, `kind`,
`claim_id`, plus `broker_order_id` for `ACCEPTED`, no extra fields for `UNKNOWN`,
or `order_evidence` for `ORDER_STATUS`. The latter is the existing
`ml.account_gameplan.authority.OrderEvidence` mapping, with `fills` as a list
of `Fill` mappings. Preserve exact fill IDs, quantities, costs, timestamps and
precision provenance; do not turn aggregate order times into execution times.

Capabilities reports identity, protocol and operations without opening books,
creating a journal, acquiring a lease, allocating capacity or proving readiness.
Other replies include protocol, request ID/hash and a status. A reserve decision
is `GRANTED` with a PR #81 grant and capacity decision, or `DECLINED` without a
grant. Replays after a state transition report the current book status; they do
not recreate permission to send. `status` never returns send authorization.

### Expired requests that never obtained admission

An expired original request can receive durable negative admission evidence
when no admission plan or prior decision exists and Atlas verifies that both
canonical books and retained reservation/send-claim history contain no matching
admission. This handles a failed capture before planning, or an original
request whose first delivery occurs after expiry. Replay the full original
payload to `reserve`; for an already stored request, `status` with its exact
request ID, hash and session can produce the same result. No fresh account or
provider capture is needed for this proof. The normal authenticated participant,
compatible-writer and canonical locked-book requirements still apply.

The immutable reply has `status: DECLINED`,
`reason: expired_before_admission`, `terminal: true`, `admitted: false`,
`send_authorized: false` and `capacity_released: false`, with the original
`session_id`, request ID and hash. Its `negative_admission_proof` contains:

* `version: intraday-negative-admission-v1`, `server_identity`,
  `account_fingerprint`, `participant_id` and `stream`;
* `request_id`, `request_sha256`, `session_id`, `valid_until` and `decided_at`;
* `canonical_account_absent`, `canonical_native_absent` and
  `send_claim_absent`, all exactly `true`.

`negative_admission_proof_sha256` is the existing protocol canonical JSON hash
of that proof. Scout must validate the authenticated original-request bindings
and durably save this explicit result before using it to clear an unresolved
PREPARED request. A transport acknowledgment or Scout's local `NOT_SENT` label
does not substitute for it. The proof revokes no reservation and authorizes no
send. Exact retries return the same decision, including after a restart or
clock rollback, and recheck canonical absence before returning it. Later
contradictory book evidence blocks the reply. Existing plans, partial pairs,
grants and uncertain claims retain their established recovery paths;
`release-unsent` is unchanged.

Retained order, submission and fill evidence also prevents an absence proof
when its canonical reservation is missing. Unattributable orphan effects fail
closed; this recovery path does not reconstruct or discard them. Unrelated
effects with intact canonical reservations do not block an otherwise valid
negative decision.

## Scout submission and recovery sequence

1. Persist an immutable request under Scout's real manual session and existing
   local strategy/quote/cutoff controls. Obtain and persist the exact grant;
   validate its authenticated account, request, quantity and expiry bindings.
2. Durably claim the local one-shot send attempt before calling
   `authorize-send`, using one stable `claim_id`. The service freshly checks
   capacity and atomically consumes a single remote claim. Only the successful
   `SEND_AUTHORIZED_ONCE` response permits that current local attempt to proceed
   through Scout's remaining manual/quote/cutoff checks to its broker adapter.
   A grant alone is never permission to POST.
3. An uncertain claim reply or uncertain broker POST leaves the local attempt
   unavailable for another send. Recover with `status` and exact broker
   evidence. A repeated claim, even with the same ID, never grants a second
   send. Expiry, a new lease epoch, a restart or a client assertion of
   `NOT_SENT` cannot release a claimed commitment or rearm it.
4. Persist and send `ACCEPTED` with the exact owned broker ID, or `UNKNOWN` when
   the outcome is uncertain. Send immutable `ORDER_STATUS` events to reconcile
   fills and terminal cancellation/rejection in both books. Reuse event IDs
   only for byte-identical retries. Durable intermediate writes repair on
   replay; inconsistent pairs block new admission until recovered.
5. Scout performs cancellation locally for its own exact order. The service
   records cancel intent without sending a broker DELETE or releasing capacity.
   Exact terminal evidence releases only the unfilled commitment; filled costs
   and native lot accounting remain recorded.

The reservation's grant epoch identifies admission history. Each service
operation uses a fresh current lease, and the once-only send claim records its
own epoch. Do not pin the PR #81 client's expected epoch to a whole manual
session. Extend Scout's adapter/journal to retain both identities rather than
relaxing current lease fencing.

## Adoption and verification

Fetch the exact published source into an isolated worktree and compare its
owned diff with the reviewed #81 base. Adopt the modified account authority,
Gameplan execution, native ledger, intraday account execution/cancellation and
runtime compatibility together with the two new service/transport modules,
four focused test modules and this document. Preserve Scout's reviewed adapter
and session work, eleven-symbol configuration, credentials, local journals and
private operating settings. Resolve overlaps explicitly; do not cherry-pick
the entire earlier PR chain again.

The focused protocol/negative-admission follow-up is based on exact PR #83,
`c994188563a547b4f711b6ada6ee359fb5b5a5b0`. Its owned changes are only the two
service/transport modules, their two test modules and this document. Reuse the
PR #83 shared accounting/stream compatibility closure. Scout's PR #82/#84
adapter, session, journal and books changes are separate sibling work to
compose explicitly; these follow-up checks do not claim to verify that combined
tree. Authenticated noncommitting writer-readiness and native accounting
projection endpoints remain Atlas-owned integration work, alongside the
production bindings above. Do not treat capabilities or the new negative proof
as evidence those endpoints or loaded writers are ready.

The completion record is the authoritative exact-byte manifest, dependency map
and check receipt. Tests use temporary ledgers, synthetic captures and fake
TLS/socket boundaries. They cover the common pool, stream isolation, once-only
claims, lost replies, crash replay, exact fills, cancellation, corruption and
loaded-code fingerprints. They do not establish a real mTLS handshake, endpoint
availability, loaded runtime compatibility or broker connectivity.

Before a human manual start, the responsible owners must verify the production
callbacks and private TLS bindings, actual compatible loaded writers, Scout's
durable adapter integration, and complete submission/fill/cancellation recovery
against the installed bytes. Keep the existing manual controls and stopped or
shadow state until that separate readiness work is complete. Offline test
counts are not trading performance evidence.
