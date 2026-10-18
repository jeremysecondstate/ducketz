# Intraday 15-minute prediction and trading system

Design record: October 8, 2026. Times in this document are America/Los_Angeles
(Pacific). Actor: Atlas. Scope: shared architecture for Atlas and Scout.

## Status and scope

This document preserves the architecture brainstorm and its settled decisions.
It describes a future system; it does not enable a trader or change operating
authority. The authorized work at this checkpoint is documentation only.
Implementation, builds, training, provider/broker calls, live orders, process
restarts, deployment, and schedule changes remain outside this checkpoint.

Current account execution bindings describe Atlas as the sole executor. Scout
has not been enabled to trade by saving this design. Current source and saved
task definitions are evidence of behavior, not instructions to run them.

Build one piece at a time: Atlas's eleven-symbol intraday prototype first,
then Scout's implementation and simultaneous cross-PC testing. Prototype/testing
authorization does not itself authorize live orders.

## 1. Three execution streams, one account

| Stream | Execution PC | Symbols | Forecast source |
|---|---|---|---|
| Existing Gameplan | Atlas | Combined 22 symbols | Nightly 1h, 4h, 1d, 1w Gameplan |
| Atlas 15m | Atlas | Atlas's eleven | Fresh local intraday predictions |
| Scout 15m | Scout | Scout's eleven | Fresh local intraday predictions |

All three use the same Schwab account. Each PC locally fetches, computes,
predicts, sizes, logs, and organizes its own intraday data in its local DATASTORE.
The human manually starts the intraday traders before/at 04:00 Pacific.

The following membership records the current configuration reviewed for this
brainstorm. Actual operating membership remains governed by each PC's local
configuration; this table is not a replacement watchlist.

| PC | Current eleven-symbol intraday scope |
|---|---|
| Atlas | AAPL, AMZN, GOOG, MU, NVDA, SNDK, COST, CROX, PATH, TWST, IONQ |
| Scout | ABCL, DBX, DOCU, GLOB, MRNA, OUST, PDYN, PYPL, QBTS, RR, SDGR |

Scout's list was read from the existing account ownership configuration,
specifically its `pc-new` participant. Atlas is `pc-original`.

## 2. Moving prediction contract

Candles are named by their opening time. Shortly after 10:15:

1. Fetch the completed 10:00-10:15 candle.
2. Use that candle's OPEN at 10:00 as the reference price.
3. Predict whether the CLOSE of the 10:30-10:45 candle, at 10:45, is greater
   than or equal to that reference open.

The probability is `P(target close >= reference open)`, called **not-down**.
Equality counts as not-down. This measures a 45-minute open-to-close return,
with approximately 30 minutes remaining when issued; the first 15 minutes
are already observed.

Update every 15 minutes using fresh completed data. Record:

- Symbol, prediction identity, and model version.
- Candle opening times and actual information availability.
- Prediction issue time and data-through time.
- Reference open price and time; exact target candle and target close time.
- Not-down probability and explicit target-definition version.

This is a moving intraday prediction, not 52 static forecasts issued overnight.
Do not create a second set of overnight-scheduled 15m trades. Nightly work can
prepare history, models, and allocation information and review completed results.
Previously issued predictions remain immutable for scoring.

Fresh inputs/inference and retraining are separate operations. Model architecture
and retraining cadence, including whether to retrain every 15 minutes, remain
unresolved. Select them during development and timed testing before production;
do not defer the specification to vague runtime conditions such as "chosen"
or "feasible."

The new not-down label differs from existing strictly-positive targets. Keep
their definitions and historical scores separate.

## 3. First quantity-sizing design

Direction prediction and quantity sizing are separate. Begin with a transparent,
symbol-specific function, not a new sizing neural network. Learned sizing can
be explored later.

Starting inputs are momentum and its changes; RSI level and slope; distance
from VWAP/moving averages; ATR relative to price; relative volume and its
changes; prediction probability; existing holdings and recent executed
quantities; and causal rates of change and acceleration of these measurements.

The starting formula is:

`15m quantity = normal hourly size for that symbol * sizing score`

The intended raw quantity is below that symbol's normal hourly quantity. The
hourly reference must not become zero merely because no hourly order is due.
Round the resulting raw quantity to the **nearest whole share**.

Still unresolved:

- The normal hourly reference for BUY versus SELL.
- Exact score range and mapping from inputs to the score.
- Half-share tie behavior.
- How to handle nearest rounding producing equality with hourly size,
  particularly when the hourly reference is one share.

Resolve that rounding issue explicitly. Do not silently substitute flooring
or introduce a minimum-trade veto. Capacity adjustment is a separate step:
a desired four-share sale can become three when only three are available.

Four quarter-hour trades may cumulatively exceed one hourly quantity; that
is acceptable for this initial design. No hourly aggregate cap is agreed.
Repeated sales may eventually close a position. There is no agreed automatic
window-expiry liquidation or forced end-of-day close.

## 4. Ownership and longer-horizon sales

| Signal | Normal owned stock | Longer allocations, in order |
|---|---|---|
| 15m | 15m | 1h, 4h, 1d, 1w |
| 1h | 1h | 4h, 1d, 1w |
| 4h | 4h | 1d, 1w |
| 1d | 1d | 1w |
| 1w | 1w | None |

15m may chip shares from its own or longer-horizon portions. Longer horizons
cannot chip shorter allocations. A sale records both the triggering signal
and the actual allocation whose shares were sold; it does not fabricate a
new purchase or erase the original ownership history.

The current [cross-horizon bearish-sale policy](CROSS_HORIZON_BEARISH_FALLBACK.md)
has a 50% shared daily ceiling, individual allocation ceilings, 18 weighted
opportunities totaling 24 weight units, and other restrictions. Its current
route permits longer-allocation sales only when normal inventory is genuinely
absent, selects at most one longer allocation per triggering forecast, and
excludes certain simultaneous instructions and pending orders.

Those are current Gameplan rules, not a finished 15m policy. Adding 15m and
access to hourly allocations requires explicit review of eligible inventory,
whether sales can span allocations, simultaneous instructions, ceiling
baselines, and pacing. No new cap percentage, pacing weights, or removal of
existing limits has been agreed. Do not carry the old opportunity counts into
15m unchanged or treat the hierarchy alone as a quantity specification.

## 5. Shared purchasing capacity and working quantities

The selected direction is **one shared pool and a shared reservation book on
Atlas, used by Gameplan, Atlas 15m, and Scout 15m**. Separate stream budgets
and advance delegation of cash/share allowances were rejected as the first
approach.

The 15m traders act independently on their signals and adjust to quantities
they can trade. The human called this their "rogue" operating style: they do
not preserve the original Gameplan position merely to keep its purchase
quantity intact. The shared book prevents two streams from committing the
same capacity. It is automatic accounting, not a human confirmation ceremony.

The working mechanism is a durable reservation before submission. Scout still
submits and manages its own orders locally; using Atlas's book does not transfer
Scout's order submission to Atlas. The actual communication interface, atomic
protocol, and restart/reconnection behavior must be specified before building
simultaneous execution. Copying local databases between PCs, or putting the
existing SQLite database on a shared drive, is not that protocol.

Each reservation identifies the account, executing stream/PC, forecast, symbol,
side, desired and permitted quantities, limit price, allocation sources, and
persistent request identity. A retry of the same request retrieves its existing
commitment rather than creating another one.

- A BUY commits the permitted quantity's cost at its submission limit and
  accounts for applicable account/exposure constraints.
- A SELL commits available shares from specifically identified eligible
  allocations. Held shares already reserved for working sales are unavailable
  for another sale.
- Broker working orders and local commitments are matched by identity and
  counted once. Unresolved submissions retain their commitments.
- Filled purchases remain accounted for while balance reporting catches up.
  Proposed sales do not create spendable purchasing capacity; account evidence
  must establish the actual available proceeds.

There are no agreed protected cash portions for Gameplan or either 15m stream.
Contention priority remains open. Serving reservations in recorded arrival
order was proposed, but has not been explicitly selected. Do not silently
grant Gameplan priority or introduce new confidence hurdles or execution vetoes.

### Quantity-adjustment example

A 15m trader wants to sell four shares. If three eligible shares are available,
it adjusts to three, reserves them, and submits. If all three fill, that
allocation closes. If three are held but two are already committed to a working
sale, only one is available for a new sale.

### Completed purchases are not replenishment targets

Atlas's Gameplan buys ten hourly shares of a Scout-list symbol. Scout's 15m
trader subsequently sells three from that hourly allocation. Scout records
actual fills locally and communicates the reduction; Atlas records seven
remaining hourly shares. The original ten-share purchase remains completed
and is not repeated to restore ten. Later instructions use actual remaining
shares and working commitments.

| Event | Hourly shares remaining | Reserved for sale | Available for another sale |
|---|---:|---:|---:|
| Ten-share purchase completed | 10 | 0 | 10 |
| Scout reserves three for a 15m sale | 10 | 3 | 7 |
| Two sell; one remains working | 8 | 1 | 7 |
| Final share sells | 7 | 0 | 7 |

## 6. Live reconciliation and after-hours exchange

Atlas checks current balances and working orders against the shared records,
logs quantity changes/discrepancies, and keeps the account books current. Each
PC preserves its own meticulous history. Shared accounting includes combined
cash commitments, shares, working orders, partial fills, and unresolved sends.

The planning assumption is that there are no other traders in the account.
The distinct symbol lists identify the responsible PC for 15m activity, but
symbol alone cannot distinguish Gameplan activity from 15m activity: Atlas's
Gameplan trades all 22. Order and forecast identities provide that distinction.

An hourly BUY of three and a 15m SELL of three can leave the balance unchanged.
Complete actual-execution records can reconstruct this after hours. A balance
snapshot alone cannot reveal both trades or their allocation effects.

| During trading | After hours |
|---|---|
| Exchange reservations, order identities, acceptance/status changes, actual fills, cancellations, and affected allocation quantities. | Exchange complete predictions, inputs, sizing explanations, execution logs, reports, and outcome data. |
| Keep cash, available shares, and unresolved quantities accurate; Atlas reconciles them with account observations. | Reconstruct activity, reconcile histories, evaluate accuracy and trading results, and integrate appropriate results into Stats/nightly review. |

The live exchange can be small; full analytical reconstruction need not happen
during trading. After-hours reports do not substitute for live commitments.
The existing five-minute nightly exchange is not yet a real-time order protocol.
Measure communication latency, including reservation and fill-report delays,
in eventual cross-PC testing.

## 7. Quote, execution, and recovery behavior

Fetch best bid, best ask, midpoint, spread, and current quote immediately before
or contemporaneously with submission. The current proposal uses the ask for
BUY and bid for SELL as marketable limit prices. Midpoint is recorded for
analysis. Spread width is not an execution veto in this proposed path.

The objective is prompt, complete execution of the adjusted quantity. A BUY
limit sets the maximum payable price; a SELL limit sets the minimum receivable
price. Execution may receive price improvement. Multiple execution prices can
produce a share-weighted average fill price; averaging is a reporting result,
not the mechanism that sets price protection.

For example, two shares bought at $100 and three at $100.05 give a $100.03
average execution price. A hypothetical SELL limit of $999 cannot execute
at $1. These figures illustrate mechanics, not current security prices.

Fresh marketable quotes do not establish actual acceptance or completion.
Record the broker's execution evidence. All-or-none/fill-or-kill conditions
are not selected for this design.

**Settled:** after partial execution, pursue only the remaining quantity.
If three shares are requested and two fill, record two filled and one remaining.
Before replacement, retrieve the old order's status and any intervening fills
so the final share cannot execute twice. Cancellation requests alone do not
release quantities. Confirmed cancellation releases only the unfilled portion.
An increased BUY limit needs its additional cost accounted for before submission.

Refresh and reprice the unfilled remainder according to an explicit policy
still to be specified. Its cadence, price updates, cancellation/replacement
mechanics, and behavior when a new prediction arrives remain open. Whether a
new prediction creates another independently reserved order or revises the
old remainder has not been settled.

For temporary provider/API failures, try, sleep a few seconds, and retry
indefinitely without an arbitrary retry-count ceiling. Respect provider-required
waiting intervals. Distinguish temporary failures from invalid requests or
code problems requiring correction.

If submission outcome is unknown, persist its identity and repeatedly retrieve
status before sending the same quantity again. Apply the same persistent
retry/sleep behavior to status retrieval. A timeout or lost connection does
not establish nonacceptance or release the commitment.

Refreshing prices is recovery work, not a terminal rejection. Test a successful
fresh quote response with unchanged prices separately from a failed response
with no usable prices. Report concrete states such as refreshing price,
retrying status, accepted, awaiting fill, partially filled, and remaining quantity.

Drop the legacy "six orders per wake" concept from this proposed design.
It has not thereby been removed from existing source.

Broker mechanics references:

- [Schwab: marketable limits and execution outcomes](https://www.schwab.com/legal/sec-605).
- [Schwab: limit-order price boundaries](https://international.schwab.com/content/stock-order-types-and-conditions-overview).
- [Schwab: average price display for multiple executions](https://help.streetsmart.schwab.com/edge/printablemanuals/edgemanual.pdf).

## 8. Last trade and next-session opening

The last intraday trade is at **16:45 Pacific**, replacing the earlier 17:00
proposal. Save the final fetched information and use it to prepare a prediction
for an immediate trade at the next eligible session's 04:00 opening.

Still unresolved:

- Exact reference price and target for that next-open prediction.
- Computation/refresh timing relative to nightly preparation.
- Whether the final 16:45 intraday forecast still targets 17:15.
- Availability of genuine target observations after the ordinary session ends.

Do not silently apply a two-candle intraday model to the overnight gap, substitute
a next-session date, or fabricate unavailable target candles.

## 9. Prototype and timing approach

When building is authorized, build and time the actual prototype with real
acquisition and inference. Measure the entire intended symbol workload alongside
Gameplan activity. Include retraining in timing if it becomes part of the
selected design. Compare measured behavior; consider 30m if 15m proves
impractical. Start with Atlas, then Scout and simultaneous cross-PC execution.

Use focused verification of recovery, duplicate prevention, partial-fill
remainders, cancellation races, reservations, accounting, and model/data
correctness. A broad simulation project is not a prerequisite.

## 10. Future UI: dedicated 15m trader tab

**Requested follow-up, deferred until the system is built:** add a new tab to
the Ducketz UI/app showing the 15m traders' activities and accuracy scores,
refreshed approximately every 15 minutes. Final name and layout remain open.

The intended information includes:

- Atlas/Scout stream and symbol, fresh forecast probability, reference open,
  exact target, issue time, data-through time, and model version.
- Desired, capacity-adjusted, submitted, filled, and remaining quantities;
  order status; execution prices and average; cash/share commitments; and
  the allocations actually reduced.
- Completed forecast outcomes and accuracy, with symbol/PC/session and
  quarter-hour filters and explicit scored/pending/missing counts.
- Latest refresh time and freshness of each PC's received information, so
  a remote view does not imply updates arrived when they have not.

Accuracy can update as new target observations arrive. A forecast whose target
has not closed is pending, not a scored success/failure. If a provisional live
comparison is displayed, label it separately from final target accuracy.
An adjusted quantity or repriced order does not rewrite the original prediction
or its target. Define the accuracy classification rule and any probability
metrics before implementing scores; an accuracy threshold is not a trading gate.
Any later correction to target data needs an auditable outcome revision while
preserving the issued prediction and prior reported observation.

This new tab supplements the previously discussed integrations:

| Existing surface | Planned 15m integration |
|---|---|
| Rolling Forecasts | Fresh forecast, issue/data times, exact target, live status |
| Gameplan | Intraday instructions/actions alongside the nightly plan |
| Stats | Saved outcomes, correct pending/missing distinctions, quarter-hour filters/grid |

The current request records the UI follow-up; it does not authorize building
the tab now. How the tab receives Scout's live data is part of the eventual
communication/UI specification.

## 11. Existing workflow context and implementation references

At the handoff, saved Atlas definitions described 21:05 Stats-first nightly
preparation, a five-minute joint Gameplan exchange with Scout synthesis,
03:35 readiness, and a 03:55 representative that reports rather than starts
trading. Priority source reconciliation ran every five minutes, and the weekly
review was Saturday 09:00. The legacy overnight task was paused; the Windows
independent stock-session task was disabled. The manual Gameplan launcher is
`Start-Gameplan-Trader.cmd`.

These are saved-definition observations, not proof a particular run completed
and not a new schedule specification. Recheck actual bindings before future
implementation. No new 15m component existed at the architecture handoff.

Relevant current project files, some under concurrent development:

- `docs/development/nightly-workflow.md` and `nightly-exchange.md`.
- `docs/loops-system-analysis/SHARED_ACCOUNT_GAMEPLAN.md`.
- `ml/nightly_workflow.py`, `ml/overnight_runtime.py`,
  and `ml/independent_stock_targets.py`.
- `ml/stock_trader/independent_session.py`, `gameplan_direction_engine.py`,
  `horizon_ledger.py`, and `catchup.py`.
- `ml/account_gameplan/authority.py`, `config.py`, and `execution.py`.

The existing reservation authority is host-local, supports the established
horizons, and assumes one submitting host. Its fill/account-balance accounting
is useful starting evidence, not an already implemented distributed service.
Do not import its current account-wide unresolved-submission blocking behavior
or other legacy restrictions into 15m without an explicit design review.

## 12. Open decisions for subsequent development

1. Model architecture and retraining cadence, selected with actual timing.
2. BUY/SELL hourly reference sizes, score mapping, and nearest-rounding details.
3. Longer-allocation quantity rules, daily ceilings, multi-allocation sales,
   and pacing with the new 15m opportunities.
4. Atomic reservation interface, communication/recovery protocol, and contention
   priority among the three streams.
5. Exact refresh/reprice/replacement policy and interaction with new predictions.
6. Final-session and next-open targets and genuine observation availability.
7. Dedicated tab layout, live remote-data delivery, and score definitions.

Resolve only these genuinely open details; do not restart or reconfirm the
settled execution arrangement, moving not-down target, transparent first sizing,
shared pool, remaining-quantity recovery, or deferred UI requirement.
