# Six-symbol hourly stock trader

Status: deployed and operator-activated on 2026-08-31. The persistent switch
remains the immediate operator stop/start control.

## Scope

The runtime trades underlying shares only for `AAPL`, `AMZN`, `GOOG`, `MU`,
`NVDA`, and `SNDK`. Its broker vocabulary is `BUY`, `SELL`, and `NO_TRADE`.
It neither constructs nor submits option instruments, and a SELL is capped at
owned shares not already committed to working sell orders. Short selling is
not part of this runtime.

Options remain a separate paper-only research lane. Any future Loop C options
shadow trader must use 1d/1w-or-longer strategies, separate receipts and
outcomes, and no Schwab options submission path.

The stock day is explicitly broader than the official regular session:

| Checkpoint session | Pacific window on an ordinary XNYS day | Schwab route |
|---|---:|---|
| `PRE` | 04:00--06:25 | `AM` limit |
| `REGULAR` | 06:30--13:00 | `DAY` |
| `POST` | 13:05--17:00 | `PM` limit |

The five-minute AM/core and core/PM transitions are closed. Early-close days
remain core-only because Schwab does not promise ordinary extended sessions on
those dates. A core decision targeting the first POST checkpoint uses the
`EXT` seamless limit route so it can be entered before PM-only entry opens.
Extended/seamless orders are always limits and fail closed when the relative
bid/ask spread exceeds `0.5%`.

Databento and broker clocks are separate. Standard-plan `EQUS.MINI` supplies
live non-empty OHLCV for the six-symbol prediction watchlist; Loop B accepts
completed source hours from 01:00--17:00 Pacific (04:00--20:00 Eastern). That
extra source context prepares the 04:30 PRE prediction but does not authorize a
Schwab order before 04:00 Pacific.

## Hourly critical path

1. Read the persistent operator switch.
2. If inactive, publish an inactive receipt and make no Schwab request.
3. Resolve the next XNYS target and wait for its unconsumed checksum-verified
   Loop B publication until 90 seconds before that target.
4. Prefer the expected fresh generation; if it is delayed, retain only an older
   still-actionable receipt for that exact target as an age-aware fallback.
5. Re-read the operator switch so a `FALSE` toggle during the wait stops before
   any Schwab request.
6. Load the current enrichment model, then fetch account/positions, working
   orders, and all six quotes concurrently.
7. Run one multi-head enrichment inference per symbol from the same snapshot.
8. Jointly convert model allocations into feasible whole-share quantities.
9. Publish the complete immutable six-symbol LIVE lane and six-symbol SHADOW
   challenger from that same snapshot.
10. If deployment execution is enabled, reserve each LIVE decision ID once and send
   its selected order immediately. Reconciliation and outcome evaluation are
   outside the pre-submit critical path.

There is no repeated confirmation ceremony or sequential checksum/reload chain
between a published eligible decision and submission. Integrity and duplicate
suppression are implemented by publishing the decision before mutation and by
an exclusive per-decision submission-intent artifact.

## ML enrichment contract

The nearest actionable Loop B 1h or 4h checkpoint is the primary direction
input, with 1h preferred only on an exact target-time tie; the other 1h/4h and
1d/1w probabilities provide context. The enrichment model also sees
spread, volume, cash/equity, current and pending symbol exposure, gross
exposure, held shares, day P/L, prediction age, time of day, and exact symbol.

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

The production switch is currently `TRUE`. Changing it to `FALSE` prevents new
submissions without changing the checked-in deployment or silently cancelling
already-working orders.

## Commands

Non-mutating decision run:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.runtime `
  --datastore-target pc
```

Regular-opening deployment command (still inert unless the operator switch is
`TRUE`):

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.runtime `
  --datastore-target pc `
  --execute `
  --queue-at-open `
  --wait-for-actionable-prediction
```

PRE-opening deployment command:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.runtime `
  --datastore-target pc `
  --execute `
  --queue-at-premarket-open `
  --wait-for-actionable-prediction
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
status, filled quantity, average fill, remaining quantity, fill count, and
sanitized per-fill quantity/price/execution-time snapshots to existing execution
events. It never persists broker fill/order identifiers and never cancels,
replaces, or submits an order.

Model fitting is deliberately separate from hourly inference and requires at
least 40 mature paired observations by default:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.training `
  --datastore-target pc
```

The bootstrap deduplicates repeated Loop B publications to the final
prospective prediction for each natural symbol/target window. The initial
production cohort contains 212 rows across 36 independent hourly target
windows and 8 sessions. Training publishes a new model artifact but never
changes the operator switch or submits an order. It uses both taken and
abstained decisions with mature counterfactual outcomes so NO_TRADE behavior
remains measurable.

After the 17:00 PT actionable stock close, the daily adaptation command audits
the PRE/REGULAR/POST live and shadow lanes, deduplicates them by Loop B
prediction, and publishes the model for the next session. New unique
observations receive weight two while the historical cohort remains the anchor:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.daily_adaptation `
  --datastore-target pc `
  --live-adaptation-weight 2
```

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
style performance. It also contains the receipt-handoff status and reports
fallback decisions separately, allowing fresh-versus-fallback performance to
be evaluated from the same mature outcomes. JSON additionally groups counts
and mature results by `PRE`/`REGULAR`/`POST` checkpoint session and by exact
Loop B target-definition version, so the broadened contract is not silently
blended with legacy regular-only evidence.

Exact broker fill reconciliation is labeled separately from the midpoint
counterfactual. The audit now also pairs receipt-matched BUY and SELL fills FIFO
by symbol across the complete immutable reconciliation history. Each closed
local lifecycle retains both decision IDs, both Loop B prediction IDs, entry and
exit sessions/timestamps/prices, matched quantity, holding time, and gross
realized P/L before unavailable broker fees. Unmatched buys remain explicit
open tracked inventory; unmatched sells remain explicit rather than being
silently forced into a round trip.

This lifecycle is labeled
`LOCAL_FIFO_STOCK_TRADER_FILLS_NOT_BROKER_TAX_LOTS`. It is more granular than
the prior forward-window estimate, but it is not Schwab's official lot selection,
fee-inclusive account P/L, or tax record. Schwab statements remain authoritative
for those claims, and the audit never blends local FIFO realized P/L with the
prediction counterfactual.

**Observed 2026-09-03 07:46 UTC:** the first production audit under this
contract verified 82 decisions, with 46 mature prediction pairs and 24 pending.
Eleven sanitized LIVE fill records contained two system BUY fills totaling 25
still-open shares and nine SELL fills totaling 101 shares with no earlier
receipt-matched system BUY lot. The FIFO result therefore correctly reported
zero closed system-owned round trips and left those sells attributed to
pre-existing/manual inventory rather than inventing realized P/L. This is an
attribution-integrity check, not a profitability conclusion. Evidence:
`C:\DATASTORE\ml\stock-trader-weekly-audits\20260903T074629.212357Z\receipt.json`.

## Production deployment

The existing `loops-hourly-operations` monitor/trainer now distinguishes the
13:00 PT official close from the 17:00 PT actionable stock close. Five stock
schedules are active:

- `Loops Stock Trader — Premarket Opening` wakes at 03:47 PT, waits for the
  04:00 PRE-opening receipt, and may queue only that exact AM target.
- `Loops Stock Trader — Opening Live + Shadow` wakes at 06:17 PT on weekdays,
  waits for the opening-target receipt, and may use the explicit opening queue.
- `Loops Stock Trader — Live + Shadow` wakes at 04:47 PT, skips the separately
  owned 05:47/opening slot, and wakes hourly from 06:47 through 15:47 PT.
- `Loops Stock Trader — Four-Hour Checkpoints` wakes at 04:17, 08:17, 12:17,
  and 16:17 PT for the 04:30, 08:30, 12:30, and 16:30 targets.
- `Loops Stock Trader — Daily Adaptation` runs at 17:20 PT on weekdays and
  publishes only after that day's complete actionable stock window closes.

The runtime's XNYS calendar blocks holidays, five-minute transitions, and
extended execution on early-close days. All six configured symbols participate
in every eligible wake, but an absent usable quote, excessive spread, lack of
trading interest, or ordinary risk gate remains an explicit `NO_TRADE` rather
than a promise of a fill.

The exact scheduler contract is
`docs/loops-system-analysis/STOCK_TRADER_AUTOMATION.md`. A production-root dry
run on 2026-08-31 published six LIVE plus six SHADOW decisions, selected live
orders, and submitted zero orders as expected without `--execute`.
