# Portable 15-minute stock-policy instructions

Jeremy's October 29, 2026 direction retires Gameplan production, Gameplan Stats,
synthesis and delivery. This policy applies to current 15-minute preparation,
model supervision and trader repairs within their existing responsibilities.
It does not restore retired Gameplan work or change local model/data recipes,
native schedules, qualification or trader activation authority.

## Direction and execution

Apply Jeremy's October 19 stock direction rules to the configured symbols on
each PC. Use the original unrounded probability: BUY at **50.01% or higher**
(`p >= 0.5001`), SELL at **49.99% or lower** (`p <= 0.4999`), and HOLD strictly
between. Retain `stock-direction-5001-4999-v3` and both exact cutoffs in applicable
new outputs. Preserve genuine data/model qualification; once a valid probability
exists, a wider confidence, assessment or promotion gate must not change its
required direction to HOLD. Do not substitute rounded probabilities or a generic
"above/below 50%" rule.

A normally activated trader uses its own current broker cash, eligible holdings,
working/uncertain reservations and live bid/ask/midpoint quotes. Retain
`execution_quantity_basis=live_broker_capacity` where required by the existing
output contract. Estimated Planning paths, price bands, guessed Execution mid
fields and projected-ledger quantities do not size, cap or gate orders. Preserve
actual capacity, own-order identity, fill attribution, known-order recovery and
idempotency. A neutral new decision does not create a fresh order; existing
known-order fill/cancel recovery remains available under its saved rules.

## Model and historical evidence

Keep model selection, fitted models, hyperparameters, calibration and
data-population recipes local to the PC's own symbols and evidence under
[independent model ownership](../development/intraday-model-ownership.md).
Shared correctness repairs preserve those choices. Model qualification and
completed-session assessment retain their actual data, chronological separation,
frozen decisions and unresolved gaps; assessment of a supervised policy does
not qualify a separate manual recipe.

Score saved 15-minute forecasts under their original policies and preserve their
probabilities, targets, labels, source lineage, timestamps and existing receipts.
Missing historical policy metadata remains unrecorded. Existing historical
Gameplan metadata does not require creation of a new Gameplan or restoration of
intentionally removed outputs. This scope change does not reinterpret historical
observations or change existing execution controls.

## Current nightly responsibility

Continue eligible 15-minute data preparation, review and model work under existing
standing authority, preserving completed stages and recovering actual technical
failures. Follow [Nightly 15-minute operations](../development/nightly-operations.md).
No Gameplan Stats, combined plan, synthesis, delivery, third account snapshot or
peer acceptance is required. Initial activation and manual stop remain with
Jeremy and the existing configured task. Scout's already-authorized iteration
replacements remain within the same logical run under
[its existing lifecycle](../development/scout-iteration-restarts.md).

This current operating scope supersedes older stock-policy material wherever
it calls for retired Gameplan production or multi-horizon nightly work.
