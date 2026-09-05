# Options Strategy paper-trading automation

> **Current deployment (2026-09-04):**
> `loops-options-strategy-paper-tracking` is paused to avoid overlapping the
> single overnight gameplan/evaluation owner. This file is a retained legacy
> contract and does not authorize a scheduled run.

This file is the durable contract for the `loops-options-strategy-paper-tracking`
standalone task in Codex Scheduled. Each wake starts in a fresh task and must
read this file completely before running the one bounded tracking command.

## Purpose and attribution

The paper lane evaluates whether Loops can select generated **Options
Strategies** better than manual selection. It does not paper-trade a generic
underlying direction or a single synthetic option. A paper entry freezes the
exact generated strategy candidate, including its strategy family, every stock
and option leg, OCC contract symbol, side, quantity, multiplier, entry BBO,
fees, capital requirement, modeled maximum loss, model scores, target window,
and source receipts.

Personal or otherwise real Schwab option history remains separate under
`ACCOUNT_OPTIONS_CONTEXT_NOT_LOOP_C_ATTRIBUTED`. It can be shown as a human or
account-context benchmark, but it must never be relabeled as a Loops paper
trade. Loop C paper outcomes remain
`LOOP_C_COUNTERFACTUAL_NOT_BROKER_EXECUTION`.

The eligible paper horizons are exactly:

- `1d`: **One-Session**;
- `1w`: **Remaining-Week Aggregate**.

The current `1w` contract is dynamic. When issued before a new exchange week,
it can span all five sessions; when issued during a week, it spans only the
remaining eligible sessions. Do not describe a midweek `1w` candidate as a
fixed Five-Session Aggregate, and do not mix it with `1w-d1` through `1w-d5`.
Those component routes are not independently paper-traded by Loop C.

## Selection and immutable entry

Loop C owns systematic selection during eligible open-session hourly wakes. It
may select only a receipt-verified candidate from the exact current Strategy
publication after the approved deterministic probability, sequence support,
expected-return, uncertainty, risk, portfolio, broker-reconciliation, and halt
gates pass. Manual favorites, UI clicks, prior realized outcomes, and current
marks after the decision time are never selection inputs.

Each `RESEARCH_PROPOSAL` stores a deterministic `paper_trade_id` plus a full
`paper_trade` snapshot in the immutable Loop C report. The snapshot remains
bound to the exact Strategy run and candidate ID. It is invalid if the target
extends beyond any option leg's expiration session or if the construction
requires a stateful lifecycle label that the current evaluator cannot produce.
No broker order payload is created.

## Expiration, exercise, and assignment

Equity option contracts can exercise or assign into stock, commonly 100 shares
per standard contract. Paper-only status prevents any real assignment, but the
simulation must not ignore the obligation:

- record every option leg's contracts, multiplier, expiration, exercise-versus-
  assignment event, and signed `BUY_SHARES`/`SELL_SHARES` obligation, scaled by
  the paper quantity; also retain gross obligation because offsetting spread
  legs can still exercise or assign differently;
- record any integral stock leg separately so covered and buy-write structures
  remain distinguishable from unsupported share exposure;
- plan to close every exact leg using the receipt-proven target-window exit BBO
  no later than its expiration session;
- if a complete exact exit cannot be proven, retain
  `PENDING_MATURE_OUTCOME_EVIDENCE` instead of assuming the option expired at
  zero or that assignment did not matter; and
- never use this paper lifecycle as authority for a future live option order.

Any future live-options implementation requires a separate production change,
direct operator approval, an earlier exit buffer, assignment/exercise handling,
buying-power controls, and broker reconciliation. This task cannot provide any
of those authorities.

## Daily schedule and command

Run at 00:17 America/Los_Angeles on Tuesday through Saturday. This follows the
prior market day's 23:42 `evaluate-strategy-outcomes` stage and gives Friday's
outcomes a final Saturday snapshot before the 09:00 weekly review.

From `C:\dev\ducketz`, run exactly once:

```powershell
.\.venv\Scripts\python.exe -m ml.loop_c.paper_ledger `
  --datastore-target pc `
  --compact
```

Parse stdout as JSON even when it exits 2. Accept `TRACKING` or
`NO_PAPER_TRADES_YET` only when `authority=OBSERVE_ONLY`,
`orders_enabled=false`, and `orders_placed=0`. The command reads only local,
receipt-verified Loop C, Strategy, and Strategy-outcome artifacts. It does not
contact Schwab or any other broker.

The command publishes an immutable run under
`C:\DATASTORE\ml\loop-c-paper-ledger-runs` and advances the checksum-bound
pointer at `C:\DATASTORE\ml\loop-c-paper-ledger-latest\run.json`. Each ledger
records open targets, mature receipt-matched outcomes, evidence still pending,
counterfactual net P/L after the Strategy evaluator's fees and spread policy,
counts by horizon/symbol/strategy family, independent decision clusters, open
potential BUY/SELL share obligations, total gross and maximum single-position
obligation, earliest open expiration, and zero-order safety.

Do not rerun merely because the result is empty. On `ERROR`, preserve the exact
error and any existing pointer, make no broker/model/risk/authority change, and
report the first invalid or missing receipt. Never rewrite or repair a pointer
by hand.

## Saturday consumption

The Saturday `loop-c-weekly-operator-review` task runs this tracker once before
the weekly review. The weekly report then includes the current ledger summary
while independently rebuilding the week's Loop C counterfactuals from exact
source receipts. A daily ledger is continuity and observability evidence, not a
substitute for the weekly receipt verification.

Final outcomes require the exact target to mature. Interim ledger checks may
say that a trade remains open or that outcome evidence is pending, but they
must not turn an interim mark into a win/loss. Saturday must keep immature
`1d` and `1w` entries separate from mature P/L.

Every response from this task ends with
`automated_action_allowed=false`, `orders_enabled=false`, and
`orders_placed=0`.
