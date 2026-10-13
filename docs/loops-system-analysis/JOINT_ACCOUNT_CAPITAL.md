# Joint Gameplan preparation and one Gameplan executor

Jeremy's October 13, 2026 standing direction defines three independent traders. Atlas runs the combined 22-symbol Gameplan trader and Atlas's local eleven-symbol 15-minute trader. Scout runs its own eleven-symbol 15-minute trader. Each trades and logs its own activity with its own local state. No trader checks, verifies, reconciles, combines, or depends on another trader.

Only the two nightly eleven-symbol Gameplans are synthesized. Use one actual account cash/stock balance, publish the same combined Gameplan and Stats on both PCs, and prepare the traders every night under permanent authorization. Atlas may synthesize directly and return the completed result to Scout without an acceptance wait. The human manually starts each trader. See [Independent trading systems](../development/independent-traders.md).

## Why the combined projection is needed

Existing local plans each start with the account's available cash and existing
account-wide exposure, but project only their own future trades. Combining
already-sized local plans would count the starting cash twice. Instead, combine
the original forecasts and planning price evidence, then run the existing cash
ledger once against one authoritative account snapshot.

The combined snapshot must cover the complete execution universe. It includes
cash already bounded by broker-visible reservations, relevant holdings,
account-wide exposure and Gameplan's own trading records. Never add two account
balances or import a 15-minute trader's local ledger.

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

Atlas's Gameplan executor uses the combined source and explicit 22-symbol execution universe while each research watchlist remains local. It reads current broker cash, shares and quotes plus its own pending orders. It does not inspect 15-minute ledgers, forecasts, health or loaded code.

Gameplan's own state lives under `state/gameplan-trader/`. Each 15-minute worker has its own `intraday/execution/` state and lock. The human manually starts the appropriate worker. Forecast identities, real own fills, conditional planning outcomes and actual broker execution remain separate records.

Both UIs display the same combined Gameplan/Stats and full account balances, with original producer lineage. Atlas can complete synthesis and publish without waiting for a peer acceptance round trip.

## Independent operating state

No native-ledger union, 15-minute ownership takeover, shared reservation service, compatibility proof or cross-trader reconciliation is required. Preserve original mixed ledgers and historical receipts; new independent state records only its own actual orders/fills. Current broker holdings provide actual sale capacity without fabricated history.

Every night is pre-authorized: produce each eleven-symbol Gameplan, synthesize once with cash/stock balances, return the same 22-symbol result to both machines and prepare the independent traders for manual start. Routine recovery does not need a new approval or verification ceremony.
