# Loop C account-equity risk calculus

## Scope and authority

This calculus filters and sizes observe-only research proposals. It cannot
submit, stage, replace, cancel, or enable an order. The pooled sequence model
supplies calibrated distributions; deterministic portfolio, broker, approval,
freshness, and halt controls retain final authority.

The risk approval binds one exact model contract:

- model: `pooled-causal-sequence-encoder`;
- policy: `pooled-causal-hourly-surface-lstm-v1`;
- configuration fingerprint:
  `0c3242b0ecbffa1a44c9a0cd2b2b3936c4c28b8e650ea22c5316887583c93e17`;
- distribution schema: `pooled-causal-distribution-v1`;
- authority: `SHADOW_ONLY`;
- consumer: `LOOP_C_OBSERVE`;
- horizons, in frozen order: `1h`, `4h`, `1d`, `1w`.

A checksum-valid sequence publication must match every field. A different
configuration, schema, authority, consumer list, or fingerprint fails before a
Loop C observation is written.

## Read-only account evidence

`ml.loop_c.schwab_snapshot` reuses the Schwab Duckets session and only calls
read-only account, order-history, and transaction-history methods. It writes an
immutable receipt-backed run under:

```text
C:\DATASTORE\accounts\schwab\loop-c-read-only-runs\<timestamp>\
```

The persisted evidence includes balances, current positions, aggregate
per-underlying exposure, working-order reserves, and aggregate closed-options
history. It excludes account numbers and hashes, OAuth tokens, and raw order,
activity, transaction, and execution identifiers. The derived current
portfolio and broker inputs are atomically written under
`C:\DATASTORE\controls\loop-c\current`.

The deployable-capital value is the minimum reported non-marginable available
funds/buying-power value, less normalized working-order cash reserves. The
source is preserved beside the value. This field is named `available_cash` in
the Loop C contract for compatibility; it is not asserted to be settled cash.

## Pending portfolio limits

Let:

- `E` be current account liquidation equity;
- `G` be current gross exposure, the sum of absolute position market values;
- `C` be deployable capital after working-order reserves;
- `S_j` be current absolute exposure to underlying `j`;
- `m_history` be a downward-only account-context multiplier in
  `{0.50, 0.75, 1.00}`.

The initial proposal uses:

```text
daily_loss_cap       = 0.0050 * E
base_trade_loss_cap  = 0.0025 * E
trade_loss_cap       = base_trade_loss_cap * m_history
gross_cap            = max(G, 1.30 * E)
symbol_cap           = 0.15 * E
position_count_cap   = current_position_count + 1
working_order_cap    = max(1, current_working_order_count + 1)
candidate_qty_cap    = 1
snapshot_max_age     = 300 seconds
model_max_age        = 5,400 seconds
```

If current gross exposure already exceeds `1.30 * E`, the formula sets the cap
to current gross exposure and leaves zero headroom for adding risk. Existing
per-symbol exposure above `0.15 * E` similarly prevents an increase in that
underlying without pretending that Loop C can liquidate or rebalance it.

The account-history throttle is deliberately asymmetric. Unavailable history,
negative reconstructed net realized P/L, closed-P/L drawdown of at least 3% of
equity, or a maximum loss streak of at least five produces `m_history=0.50`.
Immature or moderately stressed context produces `0.75`; otherwise it is
capped at `1.00`. Account history can therefore reduce a pending loss budget,
but cannot raise one.

## Horizon-specific predictive gates

The observe-only seed values are predeclared rather than chosen after seeing a
sequence assessment:

| Horizon | Strategy probability | Sequence direction | Net return on risk | Maximum total uncertainty | Uncertainty penalty |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1h | 0.60 | 0.56 | 0.03 | 0.020 | 1.25 |
| 4h | 0.60 | 0.57 | 0.04 | 0.035 | 1.35 |
| 1d | 0.62 | 0.58 | 0.06 | 0.060 | 1.50 |
| 1w | 0.65 | 0.60 | 0.10 | 0.120 | 1.75 |

These values are proposal inputs, not evidence of quality. They may filter
observe-only rows after explicit approval, but they cannot be silently tuned.
The assessment reports must show their coverage, calibration, realized net
return after modeled costs, and stability before a later change is considered.

The pilot approval normally expires Friday at 17:00 Pacific. Saturday's
receipt-verified weekly review presents the exact threshold set used and a
fresh pending equity-based proposal. Expiry and review do not imply renewal:
the next set remains inert until the operator explicitly approves its identity,
values, rationale, expiry, and independent halt state.

## Per-candidate sizing and selection

For candidate `i` on underlying `j`, let `L_i` be modeled maximum loss per
unit, `K_i` be capital required per unit, and `sigma_i` be the sequence total
uncertainty for its horizon. Define:

```text
gross_headroom  = gross_cap - G
symbol_headroom = symbol_cap - S_j

q_i = floor(min(
    trade_loss_cap / L_i,
    C / K_i,
    gross_headroom / K_i,
    symbol_headroom / K_i,
    candidate_qty_cap
))

utility_i = expected_return_on_risk_i
            - horizon_uncertainty_penalty * sigma_i
```

The candidate must pass every global, horizon, and capital gate, have
`q_i >= 1`, and have positive finite utility. The highest utility wins; the
stable candidate ID breaks an exact tie. The result remains a
`RESEARCH_PROPOSAL` with `orders_placed=0`.

No stop-loss order is configured. Observe-only records the strategy's modeled
maximum loss; that is not a promise about a future realized fill. Any future
entry, exit, stop, profit-taking, assignment, early-exercise, or reconciliation
policy requires a separately reviewed broker-execution design.

## Weekly profit-and-loss attribution

The weekly review keeps three ledgers separate:

```text
actual account closed-option P/L
    = reconstructed Schwab closes in the review period
    = real account context, not Loop C attribution

account equity bridge
    = last verified liquidation value - first verified liquidation value
    = not cash-flow adjusted and not Loop C attribution

Loop C counterfactual P/L
    = sum(receipt-matched conservative Strategy outcome * proposed quantity)
    = shadow research evidence, not a broker execution
```

A Loop C proposal enters the counterfactual sum only after its exact Strategy
candidate has a causally future, checksum-verified outcome under the existing
fee, bid/ask-spread, and exit policy. Immature horizons remain pending. Actual
Loop C realized P/L is `NOT_APPLICABLE_OBSERVE_ONLY`, with zero attributed
trades and zero orders.

## Learning without reactive model shopping

Current Schwab trade history is labeled
`ACCOUNT_OPTIONS_CONTEXT_NOT_LOOP_C_ATTRIBUTED`. It may conservatively reduce
the pending budget, but it cannot establish that Loop C works. Loop C
effectiveness begins with prospectively recorded decisions and causally mature
outcomes.

After the evidence floor, a proposed change must predeclare the cohort,
baseline, primary and safety metrics, cost assumptions, acceptance gate,
stop/retry limits, and rollback condition. It must compare immutable before and
after policies on chronological independent decision clusters. No per-trade
threshold chasing, automatic risk increase, or same-window winner selection is
allowed. A successful comparison produces only a new pending operator proposal.
