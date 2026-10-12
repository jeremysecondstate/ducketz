# Intraday 15-minute prediction and trading system

Design record: October 8, 2026; ensemble consolidation October 12, 2026.
Times in this document are America/Los_Angeles
(Pacific). Actor: Atlas. Scope: shared architecture for Atlas and Scout.

## Status and scope

This document consolidates the original architecture, the newer
[ensemble proposal](15M-ARCHITECTURE-CHANGE.md), and the October 12 human
clarifications. Development, local training, historical evaluation, shadow
forecasts, automatic scoring and the read-only development UI are authorized.
Use existing authorized data and read-only provider connections. New access,
subscriptions, credentials or expanded spending require separate authorization.
The later October 12 instruction expands development toward a manual Atlas
15m live-trader launcher: the human intends to start it after the execution
implementation is complete, observe it on Atlas, and then start Scout. Scout
may continue research/shadow runs in the meantime. The current implementation
remains forecasting/shadow plus offline execution fixtures; it is not a working
live launcher. This development does not start live sessions, enable Scout
trading, change live schedules, or restart/deploy production workers.

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

Every enabled model is fully retrained independently for every symbol every
15-minute cycle as the initial research benchmark. Daily, incremental-only,
warm-start-only, reduced-participation or older-model substitutions are not
equivalent. Timing determines subsequent experiments; any alternative cadence
is a separately identified comparison, never an automatic fallback.

Each symbol publishes immediately after its own configured ensemble finishes.
AAPL does not wait for IONQ. All its individual outputs share one immutable
cycle/snapshot/reference/target. Record configured and successful model counts;
an incomplete arithmetic mean is visibly incomplete and has no settled execution
eligibility. Primary consensus is the equal arithmetic mean of successful
probabilities. The full benchmark comprises all configured models. Probability
at or above 0.50 is not-down; this classification is not an execution-confidence
gate. Save all model successes/failures and exact versions.

Reference and target are fixed when the cycle is created. Late computation
records its actual issue time, even when the target has expired; it never
selects a later target or backdates a live issue. Historical replay explicitly
records a simulated information clock, actual computation time, and assumed
versus observed availability. Forecasts already issued remain immutable.

The new not-down label differs from existing strictly-positive targets. Keep
their definitions and historical scores separate.

## 3. First quantity-sizing design

Direction prediction and quantity sizing are separate. Begin with a transparent,
symbol-specific function, not a new sizing neural network. Learned sizing can
be explored later.

Starting inputs are momentum and its changes; RSI level and slope; distance
from VWAP, EMAs and SMAs; Bollinger Bands and oscillators; ATR relative to price;
relative volume and its changes; prediction probability; existing holdings and
recent executed quantities; and causal rates of change and acceleration of
these measurements. The October 12 clarification confirms this technical-
indicator calculus, evaluated independently for each symbol using its own data,
holdings and execution history. A shared implementation may have versioned
per-symbol indicator periods, normalization and score parameters. It must not
apply one computed score or share quantity to the whole symbol list.

The starting formula is:

`15m quantity = normal hourly size for that symbol * sizing score`

Sizing and trading opportunities occur **every 15 minutes**, alongside full
ensemble retraining. "Normal hourly size" in the earlier design is a reference
scale for quantity, not an hourly trading schedule, a requirement to wait for
Gameplan, or an hourly aggregate cap. The technical-indicator calculus and
per-symbol sizing framework are settled; the following implementation details
are not yet selected.

The intended raw quantity is below that symbol's normal hourly quantity. The
hourly reference must not become zero merely because no hourly order is due.
Round the resulting raw quantity to the **nearest whole share**.

Still unresolved:

- The normal hourly reference for BUY versus SELL.
- Versioned per-symbol indicator-to-score mappings, normalization, periods and
  BUY/SELL treatment, including the exact score range.
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

The current [cross-horizon bearish-sale policy](../loops-system-analysis/CROSS_HORIZON_BEARISH_FALLBACK.md)
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
still to be specified. Its cadence and price updates remain open.

**Settled October 12:** a new 15m forecast replaces the preceding unfilled
intention for that symbol and 15m stream **after confirming cancellation/status**.
Save the new forecast and intention while reconciliation is pending. Do not
add a second independently reserved working order for the new forecast while
the preceding commitment is unresolved. Retrieve authoritative old-order status
and record intervening fills; a cancellation acknowledgment alone is insufficient.
Unknown submissions retain their identities and commitments throughout recovery.
An unsent reservation also requires a confirmed release before reserving or
submitting the new intention. This rule does not cancel Gameplan orders or
another symbol's orders.

Reuse the existing Schwab cancellation and order-status machinery for both
Gameplan and 15m. The cancellation mechanism is shared; the reason and order
selection remain specific to each stream. A 15m new-forecast replacement must
not apply Gameplan's expiry rules or cancel another stream's order. Persist the
exact identity and cancellation request, reconcile authoritative status and
intervening fills, then release only the confirmed unfilled commitment. A
successful cancel request alone never proves cancellation. The existing live
Gameplan cancellation selector must become stream-aware before admitting 15m
orders to the shared live ledger; the offline 15m fixture is not that connection.

Completed fills and the old forecast/intention remain in the audit history.
Repricing the same intention pursues only its unfilled remainder. A new forecast
has a newly evaluated symbol-specific intention using current holdings, recent
executions and available capacity; do not copy the old quantity or automatically
subtract old fills from a newly computed quantity. Exact submission/repricing
mechanics and sizing parameters still need implementation. Replacement also
obeys the submission cutoff below.

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

**Settled October 12: 16:45 Pacific is a strict submission cutoff.** New orders,
including replacement submissions, must be submitted before 16:45:00
America/Los_Angeles on the eligible session date. At or after that boundary,
no new submission is allowed. Check actual time immediately before eventual
submission; a fetch, fit, quote or cancellation that began earlier does not
reserve permission to submit later. Account for Pacific daylight-saving time.

The cutoff limits submission, not the recording of forecasts or reconciliation
of orders already accepted. Continue recording status and actual fills after
the cutoff. A mandatory cancellation of all resting orders at 16:45 has not
been selected. A fetch beginning at/after 16:45 followed by retraining cannot
produce a timely new order.

Save the final fetched information and use it to prepare a prediction for an
immediate trade at the next eligible session's 04:00 opening.

Still unresolved:

- Exact reference price and target for that next-open prediction.
- Computation/refresh timing relative to nightly preparation.
- The final eligible intraday reference/target and whether an after-cutoff
  research-only cycle has a genuine observable target.
- Availability of genuine target observations after the ordinary session ends.

Do not silently apply a two-candle intraday model to the overnight gap, substitute
a next-session date, or fabricate unavailable target candles.

## 9. Prototype and timing approach

Build Atlas first with genuine acquisition, full model fitting and inference,
then apply the same architecture to Scout and test read-only monitoring across
PCs. Measure all eleven symbols alongside existing Gameplan activity. Report
acquisition, preprocessing, training, inference, artifact/forecast persistence,
total elapsed time, resource use, failures and overruns. Do not infer Gameplan
liveness merely from an unrelated Python process. A local-history replay is
reported as local-history replay, not measured provider publication latency.

Use all compatible historical/current data for the first measured benchmark;
the October 12 clarification rejects an arbitrary initial 90-day truncation.
Windows and model parameters remain configurable so measured timing/quality can
guide explicit comparisons. One-to-five-year experiments require genuine
compatible coverage; keep sources separate instead of manufacturing coverage.

Use focused verification of recovery, duplicate prevention, partial-fill
remainders, cancellation races, reservations, accounting, and model/data
correctness. A broad simulation project is not a prerequisite.

## 10. Read-only 15-Minute Duckets tab

After durable forecasting/scoring, build the **15-Minute Duckets** development
tab. Read actual persisted records. Individual model rows show probability
bars from 0–100%, a visible 50% reference, exact probabilities and model status/
version. Display the ensemble mean, classification, configured/successful counts,
reference, target, issue time and outcome state. Probability is not accuracy.

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

Use filters for PC, symbol, session and shadow versus historical replay. Define
today, last 20/50/100 issued forecasts, seven-day and cumulative windows with
explicit scored denominators. Show balanced accuracy, Brier score, log loss, calibration,
disagreement, model-error correlations, latency and retrospective regime
diagnostics. Realized movement regimes are retrospective; live features remain
causal. Remote receipt age and producer timestamps must be visible independently
of local refresh. No received remote data means unavailable, never fresh.

Confirmed synthetic no-trade outcomes are included in headline accuracy and
training by the October 12 human decision. Always show observed and synthetic
counts and scores separately, including equality prevalence. Missing, pending,
unscorable and provisional comparisons are excluded from scored denominators.

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

1. Model composition/parameters and useful history depth, refined with actual
   timing. Full retraining every 15 minutes remains the initial benchmark.
2. BUY/SELL reference-size bindings, per-symbol technical-indicator score
   parameters, and nearest-rounding details; the sizing cadence is 15 minutes.
3. Longer-allocation quantity rules, daily ceilings, multi-allocation sales,
   and pacing with the new 15m opportunities.
4. Atomic reservation interface, communication/recovery protocol, and contention
   priority among the three streams.
5. Exact refresh/reprice cadence and implementation of cancellation/replacement;
   new forecasts supersede prior unfilled intentions only after reconciliation.
6. Final-session and next-open targets and genuine observation availability.
7. Live remote-data delivery, worker/resource scheduling, cycle overruns and
   incomplete-ensemble execution eligibility. Explicit research settings do
   not settle eventual production policies.

Resolve only these genuinely open details; do not restart or reconfirm the
settled execution arrangement, moving not-down target, transparent first sizing,
shared pool, remaining-quantity recovery, or read-only UI requirement.

## 13. Acquisition, no-trade evidence and causal training

Acquisition is anchored to quarter-hour boundaries plus a configurable provider
publication offset. A 30-second candidate gives 10:00:30, 10:15:30, 10:30:30 and
10:45:30; it is not a repeating 15-minute-30-second interval. Thirty seconds is
an experiment, not a settled delay. Record candle close, scheduled acquisition,
request start/end, first confirmed availability, validated-data readiness,
training completion, inference and persistence. Eventual submission time remains
separate. Measure when the required interval actually becomes available.

An absent expected candle first requires classification: provider failure,
unavailable coverage, successful empty response, or closed market. Retry the
exact interval after a few seconds, respecting provider waiting rules. Temporary
failures persist and retry with sleep indefinitely; no arbitrary retry-count or
legacy twenty-minute deadline is adopted. A successful empty response alone can
be publication lag. Repeated successful empty responses plus provider evidence
that the interval is covered and has no trades permit forward-fill.

For confirmed no-trade intervals, OPEN=HIGH=LOW=CLOSE=previous known CLOSE and
VOLUME=0. Never carry the preceding candle's entire OHLC range. No prior known
price leaves the interval unavailable. Preserve source, interval, coverage,
request/retry evidence and the prior price reference. No fills across closed
sessions or nonexistent overnight targets are permitted.

For a 15m candle derived from 1m bars, each missing constituent requires its own
confirmed no-trade evidence. Aggregate the resulting fifteen constituents with
first OPEN, maximum HIGH, minimum LOW, final CLOSE and summed VOLUME. Mark an
aggregate containing any synthetic constituent; distinguish it from a wholly
no-trade flat candle. An uncovered constituent leaves the aggregate unavailable.
Late observations/corrections append revisions; retain the exact information
used in previously issued predictions.

Only labels with available target observations at the training cutoff may be
used. At 10:15:30 with data through 10:15, the newest potentially mature reference
is 09:30, target close 10:15. The fresh 10:00 reference is inference input. Fit
preprocessing on training rows only. Chronological validation purges overlapping
label windows at the fit/validation boundary. Historical availability inferred
from event times remains explicitly assumed, not reconstructed live evidence.

## 14. Forecasts and intentions survive capacity limits

Persist every valid forecast regardless of cash/shares. Save intended direction,
desired quantity, capacity-adjusted quantity and concrete execution disposition.
For example, 72% not-down, BUY 4 desired, 0 adjusted, no purchasing capacity.
Likewise retain SELL intentions when no eligible uncommitted shares exist.
Score them normally. Unresolved sizing produces explicit null quantities, not
invented sizing decisions. Failed data/model cycles have failure evidence, not
fabricated forecasts. Forecast evaluation covers all issued forecasts, separately
from any executed-trade subset. No submission means no fabricated fill or P&L.

## 15. Implementation and measured evidence

The [implementation record](../development/intraday-15m-prototype.md) identifies
the current modules, focused checks, actual data coverage and measured runs.
Its research settings are explicit experiments. Older mock sizing, book and
execution fixtures demonstrate mechanics only and do not settle production
policy, establish trained-model performance, or prove current tests pass.

The additional `ml/intraday/execution_policy.py` module is a pure offline
decision layer for the strict cutoff and confirmed prior-intention reconciliation.
It neither submits/cancels orders nor connects the offline book to the live
shared authority. Its ready state means only that these particular checks have
passed. The complete broker path, shared Gameplan/15m reservations, selected
symbol-specific score recipes and remaining execution policies are still needed
before a manual live launcher can be presented as ready.

`ml/intraday/technical_sizing.py` now implements configurable causal technical
measurements and a transparent per-symbol BUY/SELL scoring interface. It binds
to the forecast's original modern candle snapshot, retaining synthetic and
availability evidence. EMA, SMA, RSI, Bollinger Bands, stochastic, momentum,
VWAP, ATR and relative-volume parameters are explicit; no production periods,
weights, score recipe or reference sizes are selected. It returns the desired
quantity separately from the still-unconnected live capacity adjustment. The
existing shadow runner does not yet automatically persist this sizing result.

`Check-15m-Readiness.cmd` reads saved forecast evidence and local bindings and
reports the missing execution integration. It does not start a trader or call a
provider/broker. The [live preparation record](../development/intraday-live-readiness.md)
details shared reservations, cancellation reuse, loaded-worker compatibility
and exact-identity recovery work still required. The
[Scout update prompt](../development/intraday-scout-update-prompt.md) carries the
reviewed source adoption and outstanding monitoring receipt instructions.
