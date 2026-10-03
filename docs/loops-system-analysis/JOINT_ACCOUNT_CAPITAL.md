# Joint Gameplan preparation and one stock executor

## Operating design

The selected design uses two independent forecast producers and one stock
executor. Each producer keeps its frozen symbol universe, model preparation,
forecast publication and accuracy review. The designated executor consumes the
verified union of both producers' forecasts and makes one account-wide set of
trading decisions. The local human starts that executor manually after adoption
and ownership reconciliation.

This source implements the offline forecast handoff and combined capital
projection. It does not activate a trader, change a watchlist, move ownership,
grant a peer authority, or install an execution binding. Runtime integration and
the one-time ownership handoff are separate prerequisites for live adoption.

## Why the combined projection is needed

Existing local plans each start with the account's available cash and existing
account-wide exposure, but project only their own future trades. Combining
already-sized local plans would count the starting cash twice. Instead, combine
the original forecasts and planning price evidence, then run the existing cash
ledger once against one authoritative account snapshot.

The combined snapshot must cover the complete execution universe. It includes
cash already bounded by broker-visible reservations, relevant holdings,
account-wide exposure and reconciled horizon ownership. Never add two account
balances or treat two local ownership ledgers as one by copying their files.

## Producer package

`ml.joint_capital_plan.build_owner_package` seals verified native inputs into
`joint-capital-owner-package-v1`. The package contains:

- Producer identity, original run identity, target session and frozen symbols.
- Original forecasts, with their probabilities, directions, abstentions,
  information cutoffs and target windows preserved.
- Native receipt, manifest, forecast-file and planning-price-file checksums.
- Saved planning prices and their source, reference and sampling metadata.
- The frozen holding and cross-horizon fallback policies.
- Canonical JSON hashes, kept distinct from native file hashes.

A hash verifies consistency. It does not establish who generated a package.
The recipient must independently select the expected producer, universe and
package digest from its reviewed handoff. An absent generation source revision
is reported as unrecorded; the exporter must not substitute its current checkout
revision for historical provenance.

`ml.joint_capital_handoff` exports only the selected saved forecast and planning
price evidence. It verifies the native publication bindings without loading
fitted models, training cohorts or account snapshots. It creates a new private
handoff artifact and leaves native publications and latest pointers unchanged.

The package is an operating artifact, not a shared-source change. Review its
actual contents and local export authority before transport. Credentials,
ownership databases and account snapshots do not belong in a forecast package
or a portable coordination notice.

## One combined projection

`compose_joint_plan` requires exactly two packages, their independently selected
digests and universes, the target session, a designated sole executor, and one
account snapshot bound to the expected private account scope. It rejects
incomplete or conflicting membership, stale or future evidence, invalid native
forecast coverage, incompatible price contracts, invalid numeric values and
disagreeing frozen fallback policies.

The resulting `joint-capital-plan-v1` contains:

- Both immutable input bindings and the single snapshot hash and observation
  time.
- A deterministic joint forecast identity for each row, alongside its original
  producer, source run and forecast identity.
- One chronological cash ledger and explicit per-producer views of that ledger.
- The sole executor identity, global ordering policy and applied risk-policy
  fingerprint.
- Conditional planning-fill labels and explicit absence of live order authority.

The current projection uses the existing global probability/horizon/symbol
ordering. Applying that ordering across the union is recorded as
`existing_global_probability_order_v1`; it is not a new claim that independently
trained probability estimates have equal calibration quality. The live adapter
must bind the reviewed policy explicitly rather than silently introducing owner
quotas or changing order priority.

Projected sales can fund later projected purchases under the saved conditional
fill assumptions. Actual trading continues to use current broker-usable cash.
A submitted sale, expected fill, timeout, or cancellation request does not itself
make cash spendable. The joint projection is informational and is not a broker
reservation or an execution deployment approval.

Private JSON readers require bounded ordinary files under an explicit root and
reject links and reparse points. Writers create new immutable files. Readers
must be given the independently selected expected digest; reading a hash from
the same untrusted file is not independent verification.

## Execution integration contract

The executor adapter must preserve forecast and execution membership separately.
Do not globally widen `PRODUCTION_LOOPS_SYMBOLS`, `STOCK_TRADER_SYMBOLS` or a
producer's local watchlist. Those also control training and historical data.

The sole executor needs a verified composite execution source that binds both
children, their exact source receipts, original IDs, target contracts and frozen
policy. Pass its explicit union through quote capture, held-share and exposure
maps, pending-order attribution, native batch limits, preflight and final
submission checks. One native session lock, one decision batch and one reconciled
ownership ledger then govern the shared account.

The forecast-only installation must reject execution at both session startup
and direct cycle entry once its reviewed role binding is activated. A launcher
label or a changed schedule alone cannot enforce that role. Missing peer input
must never silently become a complete combined plan or authorize unrestricted
local execution.

Accuracy remains tied to each original frozen forecast. Combining plans neither
rebuilds earlier forecasts nor relabels peer results as local results. Both UIs
can display their producer's slice of the same joint plan while retaining local
actuals, pending windows and unavailable outcomes.

## Ownership takeover and readiness

If the other producer has already traded, the new sole executor must first
privately reconcile its active symbol/horizon allocations, consumed forecast
identities, fills, pending broker order IDs and unknown submissions. Broker
holdings alone cannot recover horizon ownership. Preserve both original ledgers;
do not initialize a replacement baseline over existing positions or merge
SQLite databases through a synchronized folder.

Before live activation, require all of the following evidence:

1. Both reviewed implementations are installed, with one designated executor
   and a forecast-only role enforced on the other installation.
2. Both selected packages match the same next-session date and their frozen
   universes; joint coverage is complete and the selected source policy agrees.
3. The ownership handoff is reviewed and reconciled to current broker evidence,
   including outstanding and uncertain submissions.
4. Native preflight validates the same composite source that the executor will
   consume, and one combined batch applies account limits once.
5. The human manually starts only the designated executor. Installation and
   successful offline tests do not establish a running-process version.

Missing evidence is a specific readiness limitation. It is not evidence that
the other PC failed. Existing coordination requests track technical work and
source review; Git/Drive notices do not serve as live trading locks or transport
private account state merely because they can carry JSON.
