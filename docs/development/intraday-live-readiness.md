# Atlas 15m execution preparation and readiness

Actor: Atlas. Shared engineering record, October 12, 2026. Times are
America/Los_Angeles. Read the [consolidated design](../15m-trading-system/INTRADAY_15M_SYSTEM_DESIGN.md)
and [forecasting implementation record](intraday-15m-prototype.md) together with
this record. Existing manual Gameplan controls and ownership remain in force.

## Current preparation boundary

The [pure technical-sizing engine](../../ml/intraday/technical_sizing.py) can
compute and explain causal inputs and a caller-configured sizing score. The
[read-only readiness checker](../../ml/intraday/readiness.py) reports actual
local prerequisites and blockers. Neither component is a live trader, a broker
connection, an activation grant, or a deployed account-authority upgrade.

Run the local check from the Ducketz checkout:

```powershell
& '.\Check-15m-Readiness.cmd'
```

The command is a diagnostic launcher. Its not-ready
`MANUAL_LIVE_START_NOT_IMPLEMENTED` report returns exit code 2.
It does not call providers or brokers, start workers, submit/cancel orders,
change schedules, or modify activation, reservations, holdings or credentials.
Keep its evidence local. Portable references below use `LOCAL_DATASTORE`; do
not copy databases, account identifiers or fitted models into source handoffs.

Successful individual checks describe only those checks. Presence of current
forecasting source, a valid existing account binding, or a Gameplan process does
not establish that the 15m execution adapter is implemented or loaded. Readiness
must name missing capabilities and unavailable evidence explicitly.

## Existing account and ownership contracts

The installed Atlas checkout's `ml/account_gameplan/authority.py` is a
single-host purchasing/share reservation authority. Its supported horizons are
`1h`, `4h`, `1d`, and `1w`. The existing
[native horizon ledger](../../ml/stock_trader/horizon_ledger.py) supports the same
four horizons and retains actual allocation ownership, reservations and fills.
The active databases stay at their existing host-local bindings beneath
`LOCAL_DATASTORE`; adding an independent 15m authority copy would not share
commitments with Gameplan.

The installed Atlas `ml/account_gameplan/execution.py` account bridge pins the
accepted daily Gameplan generation and source fingerprints, reconciles exact
native order identities, obtains a fenced lease and records a fresh coherent
account snapshot before reserving/submitting. Its current admission path expects
Gameplan forecasts. Its `ml/account_gameplan/config.py` configuration checks
also require the current selected Gameplan policy. These contracts do not become
15m-capable merely because a separate 15m process exists.

Those three `ml/account_gameplan` modules are installed Atlas dependencies from
separate source work. They are absent from the reviewed forecasting-source base
for this preparation and are neither included nor modified by this change.
Their exact source, local adoption and loaded compatibility must be reviewed
separately before an integration can use them. Paths above identify the
installed implementation; they are not links to files in this source stack.

The current Gameplan [runtime](../../ml/stock_trader/independent_runtime.py)
cancellation path examines working BUY reservations without an explicit stream
policy selector. Adding 15m reservations before a compatible selector is loaded
could let Gameplan apply its cancellation reasons to another stream's orders.
The intended shared mechanism includes 15m; each stream supplies its own reasons
and rules. Native horizon weights, entry windows, one-allocation restrictions,
minimum notional and other Gameplan settings are not implicitly selected 15m
policies.

No authority schema, native ledger schema, live binding or loaded worker has
been upgraded by the preparation described here.

## Required compatible integration

Use the existing Atlas authority and native ownership ledger. A reviewed
extension must identify the executing stream, forecast and exact source/model
snapshot separately from the immutable daily Gameplan context. Preserve the
daily generation/source bindings; do not overwrite them with a new quarter-hour
forecast or relabel an ensemble as a nightly Gameplan row.

Bind each authority reservation to its exact native reservation/allocation and
each accepted broker order to that persistent identity. A longer-allocation sale
must preserve both the 15m triggering forecast and the actual donor ownership.
Filled purchases remain completed after later sales; they are not replenishment
targets. Filled BUY costs remain accounted for until coherent account evidence
establishes that balances have caught up. An unfilled cancellation does not
release previously filled cash costs or erase holdings.

The intended shared operation is serialized reconciliation, fresh account
evidence, native and authority admission, then a final submission gate checking
identity, freshness, local manual authority and the strict cutoff. Native and
authority state must remain recoverable if either admission or submission is
interrupted. The eventual adapter must use the agreed 15m sizing and donor rules,
not silently inherit the legacy horizon weights or execution restrictions.

Review and install compatible readers and stream fences before introducing 15m
records into operating databases. Source publication, local installation,
schema/binding adoption and the version actually loaded by Gameplan are separate
facts. Preserve healthy workers and the human's manual start/stop controls; a
readiness check must not restart or deploy them. Scout's later order ownership
and distributed reservation protocol remain separate implementation work.

## One shared cancellation mechanism

The human selected reuse of the existing cancellation system for both Gameplan
and 15m. Implement one mechanism with stream-aware policy selectors, common
identity/authority guards, durable cancellation requests, status recovery and
reservation accounting. Do not build a second broker cancellation stack.

The existing call chain is
`independent_runtime._cancel_expired_entries` → native ledger cancellation
reservation → `SchwabSession.cancel_prepared_order` with a final `before_delete`
gate. Subsequent `horizon_broker.capture_order_evidence` normalization and native
ledger reconciliation establish status and intervening executions; the account
execution bridge reconciles the same exact order identity in shared authority.
The underlying broker cancellation API is already implemented in
[SchwabSession](../../app/services/schwab.py). Its successful DELETE does not
prove terminal cancellation or release any commitment.

The [15m offline executor](../../ml/intraday/execution.py) already exercises
`cancel` followed by status recovery through an explicit fixture venue. It is
not connected to the live broker or shared authority. Reuse its tested semantics,
not its separate fixture database as a live capacity book.

A narrow common service can receive an exact order identity and a reviewed
stream/reason decision, persist the cancellation request, run the existing
broker identity/freshness gate and existing `cancel_prepared_order` API, then
await complete status through the shared evidence/reconciliation path. Gameplan
keeps its selected timeout/expiry reasons; 15m supplies new-forecast supersession
and its separately selected repricing reasons. The current native
`reserve_cancellation` entry-grace rule and `reserve_order_timeout` five-minute
rule are Gameplan policies, not a general 15m admission method. Factor their
common persistence/accounting mechanics from their policy eligibility without
changing either stream's reasons silently.

First test this adapter only with injected broker and temporary ledger/authority
fixtures: duplicate requests, wrong stream/reason, final-gate failure, ambiguous
DELETE, cancellation still working, intervening partial/full fills, confirmed
terminal cancellation, and restart recovery. Both streams must share this
mechanism while preserving their own policy selectors. Retain commitments until
complete status proves the unfilled portion is terminal; pursue only a confirmed
remaining quantity for same-forecast repricing. This preparation does not wire
or invoke the live service.

## Exact identities and recovery

The existing Schwab submission path obtains its broker order identity from the
returned `Location`. The [order evidence reader](../../ml/stock_trader/horizon_broker.py)
requires the exact tracked broker identity and complete execution legs. It
rejects an unresolved submission without that identity and does not match by
symbol, quantity, price or nearby timestamps.

A timeout or lost returned order identity leaves the persistent native and
authority commitment unresolved. Never infer nonacceptance, release capacity or
send the same quantity again. An automatic broker-retrievable persistent request
identity or another explicitly reviewed exact recovery mechanism has not been
established by this preparation. This is a concrete live-adapter dependency,
not a reason to fabricate a successful status or fill.

Status and temporary provider failures retain sleep-and-retry behavior without
an arbitrary count ceiling. Confirm cancellation and reconcile intervening fills
before changing the unfilled intention. Repricing the same forecast's remaining
quantity is distinct from creating a new independently sized forecast intention.

## Settled policies and open decisions

The [pure execution policy helper](../../ml/intraday/execution_policy.py) implements
two settled decisions without performing orders or capacity mutations:

- New submissions are forbidden at or after 16:45 Pacific on the caller's
  independently established eligible session date. This time check alone does
  not prove the market is open or establish next-session opening targets.
- A new 15m forecast supersedes the prior same-symbol/same-stream unfilled
  intention only after exact status/cancellation reconciliation and preservation
  of intervening fills. Sizing and intention logging may occur before that
  reconciliation; reserving/submitting the new intention must wait. Old-order
  reconciliation remains useful after the submission cutoff. A new zero-sized
  intention can still supersede an old unfilled intention.

The moving not-down target, equality treatment, full retraining benchmark,
per-symbol publication, shared capacity pool, no forced position liquidation,
remaining-quantity recovery and immutable forecast/scoring records stay settled.
TA measurement definitions and pure formula mechanics do not select live weights
or execution eligibility.

The following still require explicit choices before a usable live order path:

- Positive symbol-specific normal hourly BUY/SELL references, technical-score
  weights/mappings and score range, nearest-share tie handling and hourly-size
  equality after rounding.
- Longer-allocation ceilings, spanning and pacing; shared contention rules and
  the eventual distributed reservation interface.
- Repricing cadence and exact replacement mechanics, late-cycle handling and
  incomplete-ensemble execution eligibility.
- The next eligible session's 04:00 reference/target and calculation timing.
  Genuine observation/session boundaries remain mandatory; no synthetic
  overnight or after-session targets are invented.

## Temporary-database integration verification

Build and review the compatible adapter in isolation. Use temporary authority
and native-ledger databases with explicit account/quote/status fixtures; never
open or migrate operating databases for these checks.

1. Interleave Gameplan and Atlas 15m reservations against one capacity snapshot.
   Verify atomic admission, lease fencing, freshness and no double commitment.
2. Bind every reservation to its native owner, stream and immutable forecast.
   Verify source mismatch rejection and no rewriting of daily Gameplan context.
3. Reconcile partial BUY fills and later cancellations. Confirm that only the
   unfilled portion is released and filled costs/holdings remain accounted for.
4. Attribute 15m sales to authorized donor allocations and preserve original
   purchase completion. Verify that longer signals cannot consume shorter
   inventory and Gameplan does not cancel/manage another stream's intention.
5. Interrupt before/after admission and the submission gate. Retain exact
   identities and commitments through unknown acceptance and missing broker IDs;
   never guess a match or resend.
6. Exercise new-forecast supersession, cancellation/fill races, zero-sized new
   intentions and cutoff/DST edges without copying an old remaining quantity
   into the new intention.
7. Preserve legacy Gameplan fixtures and manual controls. Verify absent live
   side effects, accurate readiness blockers and separate source/install/loaded
   runtime evidence.

Passing these offline checks is necessary engineering evidence. It is not proof
of broker acceptance, runtime deployment, a live training cycle or a completed
trade. Report those facts only when their own authorized evidence exists.
