# Portable nightly stock-policy instructions

Use this block in the existing Atlas and Scout nightly preparation, Stats,
model-review, Gameplan synthesis and delivery tasks. Preserve each native task's
PC identity, scope, schedule, settings, local bindings and existing permissions.
This is an instruction update for those tasks, not an additional workflow stage.

## Reusable task instruction block

Apply Jeremy's October 19, 2026 stock rules to all configured symbols on both
Atlas and Scout. Use unrounded probability: BUY at **50.01% or higher**
(`p >= 0.5001`), SELL at **49.99% or lower** (`p <= 0.4999`), and HOLD only
strictly between those cutoffs. Do not substitute "above/below 50%" or restore
wider probability-entry gates. This is `stock-direction-5001-4999-v3`.

The 15m, 1h and 4h horizons may sell available shares from any horizon owner.
For 1h and 4h from October 19, do not apply longer-horizon-only donors,
same-clock bullish ownership vetoes, fallback slot quotas or fallback daily
percentage caps. Keep working and uncertain sell reservations unavailable,
prevent duplicate sale of shares, and attribute actual fills to original
owners. Preserve existing 1d and 1w ownership rules. Each trader uses its own
broker capacity and manages only its own orders.

Execution uses current live broker bid/ask/midpoint quotes. Do not reintroduce
estimated Planning price or Execution mid columns or guessed-price guidance in
Gameplan. Keep fitted probabilities, model selection, hyperparameters,
calibration and data-population recipes local to the PC's own symbols and
evidence. Shared correctness repairs must preserve those local recipes.

Keep historical saved forecasts, Stats labels and policy versions, receipts,
known-order recovery and completed outputs immutable. Record the current policy
version and both exact thresholds in new Gameplan artifacts and retain them in
handoffs and completed-session Stats. Historical Stats uses the forecast's saved
policy; it does not relabel old observations using today's cutoffs. A neutral
new decision must not cause a fresh order; known-order fill/cancel recovery
remains available under the existing execution rules.

Continue the existing standing-authorized nightly work and recovery until
complete unless Jeremy explicitly stops or cancels it. Preserve completed
stages and resume unfinished stages after technical interruptions. The nightly
handoff is Atlas Gameplan plus Scout Gameplan, Scout synthesis, then delivery
to Atlas. Sending the completed result finishes the handoff. Do not require a
third account snapshot, Atlas acceptance, cross-trader verification,
reconciliation ceremony or another human approval. Actual trader starts remain
manual; this block authorizes no trading start, restart or deployment.

## Source references and artifact metadata

The authoritative shared policy is
[Stock direction and horizon autonomy](STOCK_DIRECTION_AND_HORIZON_AUTONOMY.md),
with the current operating section in [Nightly Gameplan](NIGHTLY_GAMEPLAN.md).
New artifact audit fields are `direction_policy_version`,
`direction_up_threshold`, `direction_down_threshold` and `stock_execution_policy`.
These fields record the existing rules. Their absence in a historical artifact
is not a reason to rewrite it or block nightly recovery or delivery.
