# Atlas admission host and Scout private interface

This production integration extends the coherent PR #87 tree
`5f50b2afc1967bd4115e82d8acd136c71d5aa813`, including Scout #86 and Atlas #85.
Reuse that tree and its reviewed dependency closure. Earlier forecasting,
sizing, order submission and exact fill/cancellation recovery remain in place.
The host owns account admission; Scout still submits its orders locally.
There is one shared pool, with quantity adaptation and eligible same-symbol
inventory across horizons, and no per-stream cash budgets.

Source publication, local installation, loaded writer compatibility, private
connection verification and human manual start are separate milestones.
Publishing these files does not deploy a listener or make Scout live-ready.

## Authenticated wire interface

Use the existing HTTPS client/server and mTLS trust configuration. Certificate
identity fixes the participant to `pc-new`; request fields cannot select a
participant. All POST requests require exactly one header:
`X-Admission-Protocol: intraday-shared-admission-v1`.
The enrolled server leaf SHA256 can be passed to
`AdmissionHTTPSClient(..., server_certificate_sha256=...)`; it is checked after
CA/hostname verification and before any request body is sent. Scout's client
owner must bind this to its reviewed private enrollment record.

The listener requires the native standard-library `ssl.SSLContext` directly
based on `_ssl._SSLContext`. Injected wrappers or subclasses are unsupported
for the server role and fail with `unsupported_server_tls_context`: the factory
checks before loading private certificates, and server validation checks before
binding and before each accept. This restriction does not change the client
context contract or disable a client's supported truststore integration.

Some Windows `pip_system_certs` installations replace `ssl.SSLContext` with
`pip._vendor.truststore._api.SSLContext`. Its reported verification flags alone
do not establish server-side mutual authentication. Verify the actual context
implementation in the intended listener environment before deployment. Use a
separately reviewed native server environment; never remove certificate or
hostname checks, globally replace/extract truststore, or change running traders'
TLS settings to make a server fixture pass. Strict socket-free native-server
coverage remains required alongside coverage of the actual Scout client.

Existing mutation routes are unchanged. `GET /v1/capabilities` is identity and
protocol discovery only. Its ordered `operations` list remains exactly
`capabilities`, `reserve`, `authorize_send`, `status`, `evidence`, `cancel_intent`,
`release_unsent` for compatibility with the existing strict client. The new
view routes are advertised by this separate versioned contract:

| Route | Exact request object | Response version |
| --- | --- | --- |
| `/v1/readiness` | `request_nonce` | `intraday-shared-readiness-v1` |
| `/v1/accounting` | `request_nonce`, `request_id`, `request_sha256`, `session_id` | `intraday-canonical-accounting-v1` |

All four request values are strings. Nonce/request/session identifiers start
with an ASCII alphanumeric character, continue with alphanumeric or `._:-`,
and contain at most 128 characters. Hashes are 64 lowercase hexadecimal characters. The
client generates a fresh unpredictable nonce for each observation. Additional
or missing request fields fail. JSON rejects duplicates, nonfinite numbers,
invalid encoding and bodies above 1 MiB. Errors contain sanitized codes only;
unavailable or inconsistent books never produce partial successful projections.

Both response envelopes contain:

- `version`, `protocol_version`, `server_identity`, `participant_id`, `stream`,
  `account_fingerprint`, `request_nonce` (strings).
- `observed_at`, `expires_at` (timezone-aware ISO8601 strings, at most 30 seconds
  apart; expiry is exclusive and cannot outlast the supporting writer proof).
- `authenticated: true`, `protocol_compatible: true`, `read_only: true`,
  `orders_or_commitments_created: 0` (integer zero).

Require authenticated TLS, locally pinned server/account identity, `pc-new`,
`scout-15m`, the exact original query and matching fresh nonce. Validate time
freshness with `admission_contract.validate_view_envelope`; it does not replace
TLS authentication and does not interpret a response as order authority.
The nonce prevents reuse of a response for a later local probe; it is not a
new reservation or a durable business request ID.

Readiness additionally has boolean `ready`, string-array `reason_codes`, and
`writers` with exactly `atlas` and `gameplan`. Each writer summary has boolean
`ready`, `compatibility_state` (`compatible`, `absent`, or `blocked`), string-array
`reason_codes`, and `proof_sha256` (always 64 lowercase hexadecimal characters, never null).
Writer reason arrays contain at most 32 strings matching
`[A-Z][A-Z0-9_:.-]{0,127}`. A ready writer has compatible/absent state and no
reasons; a blocked writer has blocked state and nonempty reasons. Envelope
`ready` is exactly both writers ready; its reasons concatenate Atlas then
Gameplan codes prefixed `ATLAS:`/`GAMEPLAN:` (at most 64 entries of 137 characters).
`protocol_compatible` is true independently of writer readiness.
Actual compatible loaded code or positively
verified absence is required. A missing heartbeat never establishes absence.
PID, process paths, local account records and endpoint details stay private.

Accounting additionally has the original `request_id`, `request_sha256`,
`session_id`, `status: AVAILABLE`, `snapshot_sha256`, and `projection`.
`snapshot_sha256` is `scout_protocol.canonical_sha256(projection)`; it checks
content consistency, not authentication. Canonical account/native reservations
must agree on original request scope, quantity, status, broker identity and all
exact fills. Accounting is observational and cannot release a journal item,
authorize submission, or authorize cancellation.

The projection contains:

| Section | Fields |
| --- | --- |
| `request` | `symbol`, `side`, `quantity`, `limit_price`, `forecast_id` |
| `account_reservation` | `reservation_id`, `status`, `quantity`, `filled_quantity`, `broker_order_id`, `created_at`, `last_evidence_at` |
| `native_reservation` | `reservation_id`, `allocation_id`, `status`, `quantity`, `filled_quantity`, `broker_order_id`, `last_evidence_at` |
| `fills[]` | `fill_id`, `quantity`, `price`, `executed_at`, `native_reservation_id`, `account_reservation_id` |
| `sell_slices[]` | `allocation_id`, `quantity`, `ordinal` |
| `fill_attribution[]` | `fill_id`, `allocation_id`, `quantity`, `slice_ordinal` |
| `allocations[]` | `allocation_id`, `symbol`, `horizon`, `forecast_id`, `target_start`, `target_end`, `status`, `purchase_fills`, `observed_inventory` |
| `totals` | `filled_quantity`, `gross_cost`, `gross_proceeds`, `cost_basis`, `realized_pnl`, `basis_status`, `fees` |
| `provenance` | `journal_request_sha256`, `authority_request_sha256`, `native_request_sha256`, `account_order_evidence_sha256[]`, `native_order_evidence_sha256[]` |

Quantities/ordinals are integers; prices and gross monetary amounts are decimal
strings. IDs and timestamps retain canonical identities/precision; absent broker
IDs or evidence timestamps remain null. `purchase_fills[]` contains `fill_id`,
`native_reservation_id`, `quantity`, `price`, `executed_at`.
`observed_inventory[]` contains `id`, `quantity`, `snapshot_id`, `observed_at`.
Only the request's allocation and directly attributed sell allocations appear.
Related lots can belong to an eligible other horizon; this does not grant Scout
authority over that horizon's orders.

Gross cost/proceeds come from the exact order fills. Historical fungible
allocations and opening inventory do not establish a tax-lot disposal policy.
Therefore cost basis, realized P&L and fees remain null, with `basis_status:
unknown`; purchase/assignment provenance is supplied for private reconciliation.
Do not replace unknown basis with zero or derive realized profit from test counts.

### Numeric and snapshot conformance

`projection.request.quantity` is the original requested quantity. The account
and native reservation quantities equal the admitted grant, which may be
smaller. Original request fields and the grant quantity retain their exact
request/reservation identities. Allocation status uses `ACTIVE|CLOSED`.

Every non-null price or gross monetary output is an ASCII string matching
`(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?`, at most 128 characters and at most
126 fractional places. There is no sign, exponent, leading zero, trailing
fractional zero, or rounding. Zero is `0`. The original request's limit price
retains its stricter existing 32-character/eight-fractional-place contract.
Canonical book inputs may contain bounded exponent notation; raw inputs above
128 characters or values that cannot fit the ordinary output bounds fail
before exponent expansion. Gross sums use exact integer fixed-point arithmetic
independent of the Decimal context, with cumulative quantity bounded by
`2**63 - 1`. An unsupported or overflowing amount fails the whole accounting
observation with a sanitized error; no partial, rounded or truncated success.

No source sequence or revision cursor is emitted. `version` names the schema;
`observed_at` is an observation time; `snapshot_sha256` hashes only the
projection. Keep each immutable observation. These canonical writers preserve
the original request and reservation identity, grant quantity, previously
recorded fills and per-fill attribution. Cumulative fills cannot decrease;
the authority refuses a change to an already terminal order's status or filled
quantity. Sell slices are fixed when reserved and evidence identities are
immutable. Contradictory earlier records require review, never silent replacement.

A terminal order does not freeze the entire projection. Later consistent
evidence can advance timestamps and append provenance hashes. Related lots'
purchase-fill or observed-inventory provenance can grow; a reviewed allocation
retry can reopen `CLOSED` to `ACTIVE`. Observed inventory denotes historical
assignment provenance, not currently available shares. Do not impose a
whole-projection monotonicity rule, treat terminal cache as detailed accounting
completion, or derive send/release authority from any accounting observation.

## Host and loaded writers

`AdmissionHost` composes the existing `AdmissionService`, canonical account and
native books, common `stock-trader-hourly` lock, coordinator lease and existing
`IntradayAccountExecutor._capture`. It has no order-send/cancel or model-training
operation. Only committing admission operations create book contexts; their
existing refresh path may call the broker during an explicitly deployed session.

Readiness and accounting use the same account lock but no lease, ledger schema
initialization, broker/provider call or capture. Accounting opens existing SQLite
files with `mode=ro` and `query_only`. The private host binding is checked against
Atlas's reviewed active account config, cutover and Scout's registered symbols,
and rechecked for mutation. A changed binding requires deliberate reload.

Atlas emits a process-bound proof from its actual executor at `_binding`, before
account effects and on the existing recovery cycle. Proof binds loaded method
bytecode, manual-authority/compatibility callbacks, process birth, manual session
and lock token. Reader checks fresh proof plus actual process inventory; old
loaded code, PID reuse, another writer, stale proof or ambiguous absence blocks
Scout admission. Gameplan's existing loaded-code verifier is reused. Writer proof
failure does not change Atlas's manual stop/start authority. Existing long
backoff periods may make the proof stale; peer admission then remains blocked.

`Start-15m-Admission.cmd --repository <local checkout> --datastore <local datastore>
--binding <private host-binding.json>` performs a local noncommitting readiness
diagnostic. It is not a peer TLS connection receipt. Only an explicit `--serve`
starts the HTTPS listener; adding that flag is a separate deployment operation.
Neither command starts Scout or Atlas trading. Preserve all existing workers
when adopting source; never copy/share SQLite between PCs.

## Private enrollment

Install the reviewed `admission-tls` optional dependency specification in each
intended service environment before local enrollment. Enrollment uses the
existing private nightly exchange and its locally authorized bindings. It adds
no GitHub secret transport, Drive API signal or replacement courier.

The sequence is Atlas `admission_offer` (`offer.json`), Scout `admission_csr`
(`request.json`), Atlas `admission_cert` (`response.json`), under an immutable
enrollment-ID selector. The offer binds fresh enrollment ID/nonce, server and
account identity, Scout symbols, endpoint, CA/server public certificates, pins
and validity. Scout's private key is generated locally. CSR binds the exact
offer hash/nonce; certificate response binds exact offer and CSR request hashes.
Scout verifies chain, purposes, SAN, expiry and local private-key match before
accepting a binding. Explicit previous-enrollment identity is required for
rotation. Replay and interrupted retries preserve original enrollment identity.
Only public CSR/certificate/CA and private connection configuration travel in
the private exchange. Private keys remain on their originating PC.

The explicit helper CLI is `python -B -m ml.intraday.admission_enrollment
<operation> --directory <owner-only local enrollment directory> --input
<private input JSON>`. It prints no private packet or key material.

| Operation | Input JSON |
| --- | --- |
| `offer` (Atlas) | `enrollment_id` (fresh 32 lowercase hex characters), `server_identity`, `endpoint`, `account_fingerprint`, `symbols` (Scout's actual ordered eleven symbols), optional `previous_enrollment_id` |
| `request` (Scout) | `offer` (verified payload), `trusted_offer_sha256` (receipt digest), `expected_server_identity`, `account_fingerprint`, `symbols`, optional `previous_enrollment_id` |
| `sign` (Atlas) | Exact received CSR request payload |
| `accept` (Scout) | Exact received certificate response payload |
| `host-binding` (Atlas) | Explicit private `bind_host` and integer `bind_port` |

Use `tools.nightly_exchange.publish_enrollment(config_path, enrollment_id=...,
action_date=..., review_session=..., kind=..., payload=...)` and
`receive_enrollment(config_path, enrollment_id=..., action_date=...,
review_session=..., kind=...)` with the existing locally authorized exchange
configuration. Keep the original exchange dates and enrollment identity across
retries. `receive_enrollment` returns the verified `payload` and
`trusted_payload_sha256`; an arbitrary downloaded offer is not a trust receipt.

The same operations are available through `python -B -m tools.nightly_exchange`
with `--config`, `--enrollment-kind`, `--enrollment-id`,
`--enrollment-action-date`, `--enrollment-review-session`, and
`--enrollment-receipt`. Add `--enrollment-payload` to publish the exact public
JSON; omit it to receive. Do not combine enrollment with nightly action flags.
Use a private local receipt destination: it contains private connection data.
The CLI prints only sanitized status, packet digest and zero-order count.

Atlas retains `ca.key`, `server.key`, `ca.pem`, `server.pem`, offer/request/response
JSON and `host-binding.json`. Scout retains `client.key`, `ca.pem`, `client.pem`,
offer/request/response JSON and inert `scout-binding.json`. The enrollment helper
retains the baseline `scout-atlas-authority-binding-v1` artifact. For Scout's
new `scout-atlas-authority-binding-v2` client, its local adapter constructs a
separate binding from that accepted artifact: retain every field and value,
set `version` to `scout-atlas-authority-binding-v2`, and add
`server_certificate_sha256` from the same verified offer's `server_sha256`.
That pin is the SHA256 of the server leaf certificate's DER bytes; it is not
the PEM file hash or the CA pin. Retain the existing CA/client PEM file-byte
pins. Preserve the immutable original enrollment artifacts and validate the
constructed v2 binding with Scout's reviewed client before use. No earlier
binding needs to have been provisioned. Never send either key directory as a
packet. Missing/incomplete local key state blocks retry and is
preserved for explicit recovery; no automatic new identity/key generation.

Source-only fixture checks do not
claim real certificate enrollment, endpoint deployment or private connectivity.

## Adoption sequence and remaining runtime evidence

1. Fetch the exact published completion commit into a supported isolated
   worktree. Apply its owned operations and required closure; retain Scout's
   newer reviewed client/journal/books and local eleven-symbol settings.
2. Run focused offline checks on the composed bytes, then record installation
   hashes separately. Preserve credentials, ledgers, fitted models, profiles,
   all active workers and manual controls. No bulk pull/reset/stash.
3. Responsible owners prepare private enrollment/configuration and deployment
   under their local operating authority. Check loaded Atlas/Gameplan proofs
   after a separately authorized human reload or positively verified absence.
4. Scout wires authenticated readiness/accounting and server-leaf pinning to its
   reviewed client, preserves original request/session IDs, and verifies private
   noncommitting connectivity. Complete its accounting/recovery integration and
   exact installed-byte checks. Readiness never substitutes for original grants,
   durable once-only sends, exact fills, cancellation ownership or manual start.
5. Report manual-live readiness only when these distinct prerequisites have
   explicit owner evidence. The human retains manual trader start/stop control.
