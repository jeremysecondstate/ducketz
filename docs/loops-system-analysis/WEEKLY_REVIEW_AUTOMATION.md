# Loop C weekly operator-review automation

This file is the durable contract for the separate Loop C weekly review
**standalone scheduled task**. Every scheduled wake starts in a fresh chat and
ends after its final response. No chat transcript is continuity authority.
The review is the machine equivalent of a weekly portfolio meeting: gather the
week's immutable evidence, distinguish actual account activity from Loop C
counterfactuals, calculate a pending next-period proposal, and return the
decision to the operator. It has no approval, model-mutation, or broker-mutation
authority.

The same task refreshes the exact Options Strategy paper ledger and audits
receipt-matched live-stock fills using local FIFO pairing. Personal/account
option history, Loops paper positions, and live-stock executions remain
separate evidence lanes throughout the review.

## Fresh-chat continuity protocol

Continuity is carried by an independent checksum-verified weekly scheduler
memory chain plus the authoritative review and broker receipts. It never shares
or advances the hourly guardian's scheduler-handoff chain.

1. At the start of the task, before the review command or any other workflow
   command, capture the UTC run-start timestamp and run exactly once:

   ```powershell
   .\.venv\Scripts\python.exe -m ml.loop_c.weekly_scheduler_memory read `
     --datastore-target pc `
     --compact
   ```

   Parse stdout as JSON even when it exits 2. `EMPTY` is the valid first-run
   state. `VALID` supplies only advisory prior-week context and exact evidence
   paths; revalidate them against the current checksum-valid weekly-review
   pointer, current controls, and current broker evidence. `INVALID` or `ERROR`
   is a continuity incident. Preserve it, do not trust its summary or next
   action, and never repair the chain or pointer by hand. The bounded review
   may still run because it cannot change active controls or place orders.
2. Run the bounded weekly review sequence below exactly once per command. Do
   not repeat work merely because it appears in the prior memory.
3. After the review and all verification, but before the final response, commit
   exactly one successor memory and perform no further workflow mutation:

   ```powershell
   .\.venv\Scripts\python.exe -m ml.loop_c.weekly_scheduler_memory commit `
     --datastore-target pc `
     --wake-id <captured-run-start-UTC> `
     --review-window <exact-review-window-or-UNRESOLVED> `
     --final-status <exact-review-status-or-ERROR> `
     --incident-status <NONE-or-exact-incident-token> `
     --summary <compact-summary> `
     --next-action <one-bounded-next-action> `
     [--action <completed-or-attempted-action>] `
     [--evidence <exact-path>] `
     [--changed-file <exact-path>] `
     --compact
   ```

   Include all material actions and useful review, receipt, proposal, and
   control paths. Never include account identifiers, order identifiers,
   transaction identifiers, credentials, or other secrets. Keep
   `automatic_change_allowed=false`, `orders_enabled=false`, and
   `orders_placed=0`. Parse and report `COMMITTED` or `ALREADY_COMMITTED` plus
   its sequence, pointer, receipt, and checksum. A failed commit leaves the
   prior pointer authoritative and is an unresolved continuity incident; do
   not edit or replace the pointer manually.

This chain is an advisory memory log only. Verified weekly-review receipts,
current explicit operator controls, and live read-only broker evidence remain
the operational authority.

## Schedule and review window

Run once each Saturday at 09:00 America/Los_Angeles. This is after Friday's
17:00 Pacific actionable stock close and post-close processing, and before the
next XNYS open. The runtime resolves the
most recent completed XNYS calendar week, so exchange holidays do not require
hand-authored dates.

During the observe-only pilot, approvals should normally expire Friday at
17:00 Pacific. Expiry is fail-closed; it does not trigger renewal. The Saturday
review may create a pending proposal, but only a later explicit operator
message containing identity, approval time, expiry, rationale, exact accepted
values, and independent halt state may create current controls.

## Bounded weekly review sequence

From `C:\dev\ducketz`, run exactly once:

```powershell
.\.venv\Scripts\python.exe -m ml.loop_c.paper_ledger `
  --datastore-target pc `
  --compact
```

Accept `TRACKING` or `NO_PAPER_TRADES_YET`. This is a receipt-backed paper
ledger refresh only: it must not contact Schwab or place, cancel, replace, or
simulate broker orders. Preserve its JSON, manifest, receipt, and latest-pointer
paths in weekly memory. Every recorded position must retain its exact Strategy
candidate and legs, planned target-window exit, expiration, and maximum
assignment/share obligation.

Then run the weekly review exactly once:

```powershell
.\.venv\Scripts\python.exe -m ml.loop_c.weekly_review `
  --datastore-target pc `
  --capture-schwab `
  --build-risk-proposal `
  --compact
```

Parse stdout as JSON even when it exits 2. The command:

1. resolves the latest completed XNYS week;
2. captures one fresh Schwab snapshot through the existing Duckets integration,
   using only GET account, working-order, bounded order-history, and
   transaction-history methods;
3. preserves YTD account context for conservative risk math and an exact
   weekly subperiod for the review;
4. verifies all published Loop C observe runs in the week and requires zero
   order authority;
5. joins selected shadow candidates only to causally mature, receipt-matched
   Strategy outcome evidence when available;
6. creates a fresh `PENDING_OPERATOR_APPROVAL` risk proposal; and
7. publishes JSON and Markdown review artifacts with an immutable receipt and
   latest pointer.

Before the stock audit, reconcile any submitted stock decisions exactly once
through Schwab's read-only recent-order history:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.reconciliation `
  --datastore-target pc
```

Accept `RECONCILED`, `PARTIAL_RECONCILIATION`, or `NO_SUBMITTED_ORDERS`. This
command may only read broker order history and append immutable sanitized fill
snapshots; it must not submit, cancel, or replace an order.

Then run the separate stock-trader decision/outcome audit exactly once:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.audit `
  --datastore-target pc
```

The audit resolves the same latest completed XNYS week, verifies every
stock-trader decision receipt, and joins each stable `decision_id` and its
reason/order-style explanation to the causally mature Loop B evaluation. It
must retain BUY, SELL, and NO_TRADE decisions, including hypothetical quantity
economics for abstentions. Review counts and mature results separately by
`PRE`, `REGULAR`, and `POST` checkpoint session and by exact target-definition
version; never blend the new extended contract with legacy regular-only rows
when making a comparative claim. Accept `WEEKLY_AUDIT_COMPLETE`, `OUTCOMES_PENDING`,
or `NO_STOCK_TRADER_DECISIONS`. This command is evaluation-only: it must not
contact Schwab, submit an order, activate the trader, train a model, or mutate
policy. Preserve its JSON, Markdown, manifest, receipt, and latest-pointer
paths in the weekly memory.

The stock audit also reports receipt-matched fill lifecycle evidence. Pair BUY
and SELL fills FIFO by symbol, retain the entry and exit decision/prediction
IDs, and report gross realized P/L before unavailable broker fees. Label this
`LOCAL_FIFO_STOCK_TRADER_FILLS_NOT_BROKER_TAX_LOTS`; unmatched inventory stays
open and must not be counted as realized. Schwab statements remain the
authority for official tax lots, fees, and account P/L.

Do not run `ml.stock_trader.training` from this scheduler. A weekly audit can
inform later model work but cannot turn one week into an automatic refit or
authority change.

Accept review statuses `WEEKLY_OPERATOR_DISCUSSION_READY`,
`INSUFFICIENT_MATURE_LOOP_C_OUTCOMES`,
`INSUFFICIENT_LOOP_C_OBSERVATIONS`, or
`INCOMPATIBLE_COHORT_DEFINITIONS`. Insufficient evidence is an honest weekly
result, not a failure and not permission to loosen a gate. Treat `ERROR` as a
review incident and preserve its exact error; do not improvise around a failed
receipt or rerun with weakened verification.

## Required attribution split

The review must never blend these four quantities:

- `actual_account_context`: reconstructed closed Schwab option positions for
  the week. These are real account results but are labeled
  `ACCOUNT_OPTIONS_CONTEXT_NOT_LOOP_C_ATTRIBUTED` unless future execution
  receipts explicitly link them to Loop C.
- `equity_bridge`: first-to-last verified liquidation-value change. It includes
  market movement and may include cash flows, so it remains
  `UNATTRIBUTED_ACCOUNT_EQUITY_CHANGE` until cash-flow reconciliation exists.
- `shadow_counterfactual_performance`: what receipt-matched Loop C research
  proposals would have produced under the checked-in conservative Strategy
  exit, fee, and spread assumptions. This is labeled
  `LOOP_C_COUNTERFACTUAL_NOT_BROKER_EXECUTIONS`; it is not money made or lost in
  the account.
- `options_strategy_paper_tracking`: the cumulative receipt-backed paper ledger
  for exact Loops-selected 1d/1w Options Strategies, including open, pending,
  and mature positions. It is labeled paper-only and is never merged with the
  operator's historical Schwab option positions or treated as broker activity.

For immature 1h, 4h, 1d, or 1w targets, report `PENDING_OR_UNAVAILABLE` rather
than using current marks as final outcomes. Never manufacture an outcome from
the direction prediction alone.

## Operator discussion boundary

Present the operator with:

- actual weekly closed-option P/L, wins, losses, drawdown, and strategy and
  underlying breakdowns;
- Loop C run, proposal, no-trade, halt, reason-code, horizon, and symbol counts;
- Loop C option-paper outcomes only for 1d and 1w candidates; 1h/4h options are
  outside the approved paper lane;
- treat `1w` as the Strategy system's dynamic `Remaining-Week Aggregate`, not
  as an assumed fixed five-session contract when the candidate is issued after
  Monday;
- show each paper position's standard-contract multiplier, contract quantity,
  signed potential `BUY_SHARES`/`SELL_SHARES` obligation, gross obligation,
  expiration, and target-window exit no later than expiration session; any
  missing bounded exit is ineligible for entry rather than accepted
  as assignment risk;
- mature counterfactual P/L after modeled execution costs, with pending outcomes
  kept separate;
- exact model fingerprint, risk approval ID, expiry, policy version, and exact
  threshold/risk sets used during the comparable cohort;
- the new pending proposal's exact limits and artifact paths; and
- a recommendation of hold, tighten, halt, or preregister one bounded change.

The task may brainstorm with the operator, but it must not write
`risk-approval.json`, write an unhalted `halt-control.json`, change a threshold,
swap a model binding, retrain a model, promote a challenger, or call a broker
mutation. A favorable week cannot raise risk automatically. A poor week can
support an operator decision to tighten or halt, but the task still cannot
apply that decision without explicit approval.

## Relationship to hourly model work

The hourly task performs current inference and receipt-first health/drift
monitoring. It does not refit the pooled encoder every hour. Model outcomes have
different causal maturity clocks: an hourly refit would repeatedly tune on
overlapping and often immature labels.

The existing staged scheduler remains the model-improvement path:

- hourly: inference, freshness, lineage, calibration and drift monitoring;
- nightly stage 13: preregister one exact hypothesis and frozen cohort;
- nightly stage 14: run that one bounded shadow ablation;
- stages 15-16: compare and stress the unchanged challenger;
- the existing 22:00 UTC Strategy-profit owner: sole checked-in production
  retraining/promotion path for its current 1d/1w scope; and
- weekly operator review: interpret comparable mature evidence and decide
  whether another proposal is worth testing.

This preserves adaptive learning without turning one noisy hour or one lucky
week into an unreviewed model or policy change.

## Final task response

Lead with the memory sequence, memory receipt, week, status, review artifact,
review receipt, paper-ledger artifact, and pending proposal. Then give the
personal/account option history, Loops Options Strategy paper performance,
Loop C counterfactuals, and live-stock FIFO lifecycle results under explicit
labels. State how many outcomes are still immature, whether model/risk cohort
definitions were comparable, the maximum open paper share obligation, and the
single most defensible next discussion item. End with
`automatic_change_allowed=false`, `orders_enabled=false`, and
`orders_placed=0`.
