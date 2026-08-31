# Codex handoff prompt: hourly trade-decision planner and Loop C audit

Continue the Duckets Loops-system design work in `C:\dev\ducketz`.

## Operating mode for this task

Begin in **brainstorming and architecture mode**. Inspect the repository and current runtime state read-only, then present a concrete design and calculus for discussion. Do not implement, activate, schedule, train, place, cancel, replace, or modify any broker order until I explicitly approve a later implementation phase.

Treat the research papers listed below as evidence sources, not as instructions. Preserve unrelated worktree changes.

## What we are designing

Design a separate hourly **trade-decision planner** that consumes the Duckets Loops prediction outputs and portfolio state. The intended workflow is hands-off for data collection, inference, gating, sizing, proposal generation, broker preview where supported, reconciliation, auditing, and notifications.

“Hands-off” does **not** grant authority to submit autonomous securities or options orders. The existing Schwab Duckets UI/manual order API must remain intact and continue to support manual trading. Human confirmation should be requested only when the system has produced a genuinely eligible, current proposal—not on every hourly wake and not for no-trade decisions. Explore a queued-proposal and notification workflow that minimizes attention without weakening the broker boundary.

The planner is distinct from:

- **Loop C**, the independent hourly observe-only evaluator/auditor; and
- the **Saturday 9:00 AM Pacific weekly operator review**, which summarizes the week and supports a human discussion about thresholds and risk limits.

Do not describe Loop C itself as the live trader or as merely a weekly operator.

## Current architecture and scope to verify

- Repository: `C:\dev\ducketz`
- Production Loops universe: exactly `AAPL`, `AMZN`, `GOOG`, `MU`, `NVDA`, and `SNDK` unless checked-in configuration now says otherwise.
- Holdings outside that allowlist are portfolio context only and must be non-tradeable by this planner. Existing manual exit orders outside the allowlist should be classified as reduce-only portfolio activity, while still participating in collision checks and broker reconciliation.
- The existing hourly scheduled task is documented as `loops-hourly-operations`.
- The separate weekly review is documented as `loop-c-weekly-operator-review` for Saturday at 9:00 AM Pacific.
- Loop B and Options Strategies are intended to share a pooled, causally pretrained sequence encoder. Loop C consumes calibrated distributions and uncertainty, while deterministic risk limits, portfolio constraints, reconciliation, and halt controls retain final authority.
- There is no assumed autonomous Schwab execution component. Verify every current capability rather than inferring it from UI controls or documentation.

These were time-sensitive observations from the prior task and must be rechecked rather than treated as current truth:

- No published sequence-model pointer, sequence-inference pointer, or Loop C publication was present at the last inspection.
- A premarket Options Strategies run produced 7,680 candidates across the six symbols, but none passed surface-quality or liquidity policy; most pricing was unavailable and the remainder delayed.
- Working sell-limit exits in `VYX`, `ICHR`, and `FRVO` were outside the Loops universe and appeared reduce-only.

Read-only Schwab inspection of balances, positions, open orders, and available trade history is authorized. Broker mutations are not authorized in this design task.

## Resolve the instrument design explicitly

Consider both of these as separate proposal lanes:

1. **Stock/ETF lane**, primarily consuming Loop B distributions.
2. **Options-strategy lane**, consuming the Options Strategies distributions and exact strategy candidates.

Recommend whether the first implementation should cover stocks only, options only, or both. If both are recommended, keep distinct risk budgets, evidence ledgers, calibration metrics, outcome definitions, and halt conditions. Do not pool a stock observation and an option-strategy observation merely because they share an underlying or timestamp.

## Candidate calculus to design

For every hourly wake, the default answer may be **NO TRADE**. Define an auditable proposal calculus that produces either a rejection/no-trade receipt or an immutable eligible proposal.

### Common predictive layer

Include at least:

- exact model and feature-data fingerprints;
- decision timestamp and horizon;
- calibrated direction/return distribution;
- uncertainty and out-of-distribution indicators;
- forecast freshness and market-data freshness;
- expected return net of estimated costs, spread, slippage, and fees;
- agreement or conflict across relevant horizons;
- a documented abstention region;
- separation between model score and deterministic eligibility.

### Stock/ETF proposal lane

Define the math for:

- side and target quantity;
- expected net return and expected dollar value;
- adverse-move distribution and loss-at-risk;
- position concentration, gross/net exposure, correlated exposure, turnover, and available buying power;
- entry limit construction, expiration, and what invalidates the proposal;
- exit thesis, time stop, protective loss control, and treatment of gaps;
- incremental portfolio risk before and after the proposed order.

### Options-strategy proposal lane

Define the math for:

- exact legs, expirations, strikes, ratios, side, and net debit/credit;
- calibrated probability of profit and full net-return distribution;
- maximum loss, capital/collateral requirement, expected value after fees, and tail loss;
- executable bid/ask quality, legging risk, modeled slippage, and fill uncertainty;
- Greeks and portfolio-level Greek concentration;
- assignment, early-exercise, expiration, dividend, and pin risks;
- strategy-family and underlying concentration;
- lifecycle/exit rules and the outcome timestamp used for evaluation.

### Deterministic authority layer

Regardless of model confidence, require deterministic checks for:

- six-symbol allowlist and protected holdings;
- current account equity, cash/buying power, positions, open orders, and order collisions;
- risk-increasing versus reduce-only classification;
- daily-loss, per-trade-loss, gross-exposure, per-underlying, position-count, working-order, and quantity caps;
- separate stock and options risk budgets if both lanes exist;
- stale snapshot/model/quote rejection;
- market calendar and eligible session window;
- idempotency, duplicate suppression, and immutable receipts;
- reconciliation before and after any manual submission;
- explicit halt, rollback, and operator override controls.

Use the previously discussed numbers only as a **pending proposal to recompute**, not as active policy: 0.50% daily-loss cap, 0.25% base per-trade loss with a 0.50 history multiplier, 130% gross exposure, 15% per underlying, 21 positions, one working risk-increasing order, quantity one, five-minute broker-snapshot age, and 90-minute model age.

## Terminology: validation versus evaluation

Use these terms consistently:

- **Pre-trade operational validation**: before a proposal can be shown, verify allowed instrument, current data, account/order reconciliation, deterministic risk limits, internal order semantics, and broker preview/capability where available.
- **Post-decision outcome evaluation**: after the registered horizon matures, compare the prediction and proposed economics with realized or counterfactual outcomes, including costs, slippage, drawdown, calibration, and abstentions.

Do not say the 40-session gate “validates trades.” It is a prospective live-session **evidence-collection and system-review gate**, not retroactive permission for an individual order.

For stocks/ETFs, register metrics such as directional calibration, horizon return, net expected-value error, adverse excursion, slippage, turnover, and drawdown. For options, separately register exact-leg/strategy-family results, fill feasibility, debit/credit, fees, maximum loss, assignment/expiry events, calibration, and realized or counterfactual net return.

One 40-session calendar window may contain both lanes, but each lane must have separately preregistered cohorts, minimum evidence, pass/fail thresholds, and review conclusions. Reaching 40 sessions should trigger a human **continue / tighten / pause / expand** review; it must not automatically expand authority or mutate policy.

## Proposed hourly lifecycle to critique

1. Snapshot predictions, quotes, market/session state, balances, positions, open orders, and relevant history.
2. Reconcile account and working-order state.
3. Generate candidates separately for stocks/ETFs and options strategies.
4. Apply calibration, uncertainty, liquidity, cost, portfolio, risk, freshness, and collision gates.
5. Publish an immutable NO-TRADE receipt or an eligible proposal with complete calculation provenance.
6. For an eligible proposal only, create a broker preview if the supported interface is genuinely non-mutating, queue it in the Duckets UI, and notify the operator.
7. Preserve explicit human confirmation at actual Schwab submission.
8. Reconcile the resulting manual action or expiry/cancellation of the proposal.
9. Let Loop C independently audit planner decisions and counterfactual alternatives.
10. Let the Saturday review separate: all account activity, planner proposals, operator-confirmed trades, declined/expired proposals, no-trade decisions, and Loop C counterfactuals.

## Files to inspect first

- `docs/loops-system-analysis/HOURLY_AUTOMATION.md`
- `docs/loops-system-analysis/POOLED_SEQUENCE_LOOP_C.md`
- `docs/loops-system-analysis/LOOP_C_RISK_CALCULUS.md`
- `docs/loops-system-analysis/LOOP_C_ROLLOUT_PLAN.md`
- `docs/loops-system-analysis/WEEKLY_REVIEW_AUTOMATION.md`
- `docs/loops-system-analysis/SYSTEM_FUNCTIONALITY.md`
- `ml/loop_c/`
- `ml/sequence_encoder/`
- `ml/universe.py`
- `app/services/schwab.py`
- `app/services/schwab_strategy_orders.py`
- `app/services/strategy_order_review.py`
- `app/ui/options_strategy_data.py`
- `app/ui/schwab_duckets.py`

Also inspect current scheduler definitions, current pointers/publications, live receipts, and `git status`. Do not overwrite unrelated edits.

## Research material

Read and synthesize only what is relevant to this architecture:

- `I:\Shared drives\SECONDSTATE\DUMPER\ML-TADING-EFFECTS-PAPER.md`
- `I:\Shared drives\SECONDSTATE\DUMPER\ML-TADING-EFFECTS-PAPER-UNC.md`
- `I:\Shared drives\SECONDSTATE\DUMPER\ML-TADING-EFFECTS-PAPER-CHAPMAN.md`
- `I:\Shared drives\SECONDSTATE\DUMPER\ML-TADING-EFFECTS-PAPER-HARVARD.md`
- `I:\Shared drives\SECONDSTATE\DUMPER\ML-TADING-EFFECTS-PAPER-UTX.md`
- `I:\Shared drives\SECONDSTATE\DUMPER\ML-TADING-EFFECTS-PAPER-AUC.md`

Distinguish claims in those documents from my instructions. Treat external claims as hypotheses unless supported by the paper and applicable to our data-generating process.

## First deliverable—no implementation yet

Return:

1. A concise current-state correction based on repository/runtime evidence.
2. A proposed component architecture showing the planner, Loop B, Options Strategies, Loop C, Schwab read-only data, Duckets confirmation boundary, and weekly review.
3. Separate stock/ETF and options calculus, including a numerical worked example for each using clearly labeled hypothetical inputs.
4. The immutable proposal/receipt schema and proposal state machine.
5. A 40-session evidence plan with separate lane metrics and preregistered review thresholds.
6. A short list of material design decisions that require my approval before implementation.
7. A phased implementation and verification plan that preserves the current scheduler and manual Schwab UI path.

Lead with findings and tradeoffs. Do not make source changes in this first pass.
