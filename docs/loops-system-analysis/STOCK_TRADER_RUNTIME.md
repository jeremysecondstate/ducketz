# Six-symbol hourly stock trader

Status: implemented, default-disabled, not deployed or activated.

## Scope

The runtime trades underlying shares only for `AAPL`, `AMZN`, `GOOG`, `MU`,
`NVDA`, and `SNDK`. Its broker vocabulary is `BUY`, `SELL`, and `NO_TRADE`.
It neither constructs nor submits option instruments, and a SELL is capped at
owned shares not already committed to working sell orders. Short selling is
not part of this runtime.

Options remain a separate paper-only research lane. Any future Loop C options
shadow trader must use 1d/1w-or-longer strategies, separate receipts and
outcomes, and no Schwab options submission path.

## Hourly critical path

1. Read the persistent operator switch once.
2. If inactive, publish an inactive receipt and make no Schwab request.
3. Load the receipt-verified current Loop B publication and current enrichment
   model.
4. Fetch account/positions, working orders, and all six quotes concurrently.
5. Run one multi-head enrichment inference per symbol from the same snapshot.
6. Jointly convert model allocations into feasible whole-share quantities.
7. Publish the complete immutable six-symbol decision run.
8. If deployment execution is enabled, reserve each decision ID once and send
   its selected order immediately. Reconciliation and outcome evaluation are
   outside the pre-submit critical path.

There is no repeated confirmation ceremony or sequential checksum/reload chain
between a published eligible decision and submission. Integrity and duplicate
suppression are implemented by publishing the decision before mutation and by
an exclusive per-decision submission-intent artifact.

## ML enrichment contract

Loop B's actionable 1h calibrated probability is the primary direction input;
4h, 1d, and 1w probabilities provide context. The enrichment model also sees
spread, volume, cash/equity, current and pending symbol exposure, gross
exposure, held shares, day P/L, prediction age, and time of day.

The model emits:

- probability that an action is worth taking;
- target allocation/liquidation fraction (the primary order-size control);
- expected net return after waiting, spread, slippage and costs;
- adverse return and protective distance;
- execution urgency, limit aggressiveness, and expected holding time.

Deterministic arithmetic only enforces reality: available cash, gross and
per-symbol capacity, whole shares, current working orders, and available owned
shares for a SELL. It cannot invent a larger allocation than the model emits.

Order style is selected from urgency:

- low: passive limit;
- moderate: midpoint limit;
- high: marketable limit;
- very high: market only when the versioned policy explicitly permits it,
  otherwise a marketable limit;
- weak expected value: no order.

Every branch stores a stable reason code and plain-language explanation.

## Operator switch

The production location is:

`C:\DATASTORE\controls\stock-trader\operator-intent.txt`

It must contain exactly one nonblank line:

```text
CONFIRM_ACTIVE_TRADING=FALSE
```

or:

```text
CONFIRM_ACTIVE_TRADING=TRUE
```

Missing, unreadable, malformed, or `FALSE` means inactive. `TRUE` permits the
deployed trader to act without per-order human intervention. The runtime also
requires the deployment command's `--execute` flag, preventing an undeployed
developer invocation from becoming active merely because the persistent
production switch is true.

No production switch was created or changed as part of implementation.

## Commands

Non-mutating decision run:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.runtime `
  --datastore-target pc
```

Deployment command (still inert unless the operator switch is `TRUE`):

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.runtime `
  --datastore-target pc `
  --execute
```

Weekly paired audit for the latest completed XNYS week:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.audit `
  --datastore-target pc
```

Read-only reconciliation of prior submissions (run after the critical path or
on the following wake):

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.reconciliation `
  --datastore-target pc
```

This makes one bounded recent-order-history read and appends immutable broker
status, filled quantity, average fill, remaining quantity, and fill-count
snapshots to existing execution events. It never cancels, replaces, or submits
an order.

Model fitting is deliberately separate from hourly inference and requires at
least 40 mature paired observations by default:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.training `
  --datastore-target pc
```

Training publishes a new model artifact but never changes the operator switch
or submits an order. It uses both taken and abstained decisions with mature
counterfactual outcomes so NO_TRADE behavior remains measurable.

## Decision-to-reality audit

Each hourly decision has a stable `decision_id`. The weekly audit joins its
Loop B `prediction_id` to the receipt-verified mature evaluation and stores the
decision explanation beside:

- the observed forward raw return;
- the BUY/SELL-direction-aligned net return after the registered cost;
- selected and hypothetical quantity result dollars;
- expected-value error; and
- submission status when one exists.

When reconciliation has observed fills, the pair also includes exact filled
quantity, weighted-average entry fill, midpoint slippage, broker status, and a
conservative fill-slippage-adjusted result. Pending/unfilled orders remain
explicit rather than being treated as trades.

Thus a record such as
`WEAK_EXPECTED_VALUE_AFTER_WAITING_AND_SLIPPAGE -> NO_TRADE` remains directly
paired with what the market subsequently did. JSON contains the full pairs;
Markdown contains a compact row-by-row audit table and grouped reason/order-
style performance.

Exact broker fill reconciliation is labeled separately from the midpoint
counterfactual. Even with a fill, the registered forward market outcome is not
mislabelled as tax-lot/account P/L; that stronger claim requires a matched exit
or position-history attribution.

## Deployment boundary

Implementation did not edit the existing `loops-hourly-operations` scheduler,
write the production TRUE/FALSE file, publish a production enrichment model,
or call Schwab. Deployment should first run the command without `--execute`,
review receipts, publish a trained model, add the `--execute` command to the
hourly owner, and only then use the operator file to activate it.
