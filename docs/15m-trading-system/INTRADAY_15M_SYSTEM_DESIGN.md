# Intraday 15-minute prediction and trading system

Design record: October 8, 2026; ensemble consolidation October 12, 2026.
Times in this document are America/Los_Angeles
(Pacific). Actor: Atlas. Scope: shared architecture for Atlas and Scout.

## Status and scope

This design incorporates the October 13 standing human correction. It defines
independent local traders and the recurring two-plan Gameplan handoff. Earlier
shared-service and loaded-peer-writer requirements are retired operating policy.
The exact moving target, full configured ensemble, independent technical sizing,
local history and truthful outcome accounting remain unchanged.

Every night is already authorized by Jeremy's October 13 standing instruction:
Atlas produces its Gameplan, Scout produces its Gameplan, Scout synthesizes the
two plans, and Scout sends the result to Atlas. Delivery completes the handoff.
No further human permission, peer acceptance, third account snapshot or
cross-trader verification/reconciliation is an operating prerequisite. Each of
Gameplan, Atlas 15m and Scout 15m executes independently and keeps its own logs.
The first trader start remains Jeremy's manual action. Source publication and
private-data procedures continue separately from this operating authority.

Scout's current manual launcher defaults to `broker-local`. It uses the existing
local broker account directly at human startup, without Gameplan configuration,
Atlas enrollment or a remote service. Engineering tests and saved research runs
are evidence for their actual source/results; they do not claim a live order.
Use existing authorized connections and retain private credentials locally.

## 1. Three execution streams, one account

| Stream | Execution PC | Symbols | Forecast source |
|---|---|---|---|
| Existing Gameplan | Atlas | Combined 22 symbols | Nightly 1h, 4h, 1d, 1w Gameplan |
| Atlas 15m | Atlas | Atlas's eleven | Fresh local intraday predictions |
| Scout 15m | Scout | Scout's eleven | Fresh local intraday predictions |

All three use the same Schwab account. Each PC locally fetches, computes,
predicts, sizes, logs, and organizes its own intraday data in its local DATASTORE.
The human manually starts each intraday trader. Startup selects the latest
eligible completed candle on the quarter-hour grid, using the configured
publication offset; it does not wait for a Gameplan instruction or trade a
backlog. The special immediate 04:00 next-session prediction remains unresolved.

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
an incomplete arithmetic mean is visibly incomplete. The initial manual Atlas
executor admits only the complete configured ensemble; research retains partial
outputs without inventing an order fallback. Primary consensus is the equal
arithmetic mean of successful probabilities. The full benchmark comprises all configured models. Probability
at or above 0.50 is not-down; this classification is not an execution-confidence
gate. Save all model successes/failures and exact versions.

Reference and target are fixed when the cycle is created. Late computation
records its actual issue time, even when the target has expired; it never
selects a later target or backdates a live issue. Historical replay explicitly
records a simulated information clock, actual computation time, and assumed
versus observed availability. Forecasts already issued remain immutable.

The new not-down label differs from existing strictly-positive targets. Keep
their definitions and historical scores separate.

## 3. Approved independent technical sizing

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

**Settled October 12:** the first live configuration uses an independent
per-trade scale, replacing the earlier normal-hourly-size dependency:

`reference shares = 0.015 * current account equity / fresh side price`

`desired shares = nearest_whole_share(reference shares * technical score)`

BUY uses the fresh ask and SELL the fresh bid. The score lies in `[0, 1)`;
nearest-share rounding uses half-up (`0.5` rounds up), and rounded equality with
the reference share count is permitted. Rounding can exceed a fractional
reference; it is not clipped back to that reference. There is no flooring
substitution, minimum-share veto, Gameplan due-order prerequisite, hourly aggregate cap,
stream budget, protected cash portion or reserve buffer. The 1.5% scale defines
this intention's size; it does not earmark account money for that stream.
Trading opportunities and full ensemble retraining remain **every 15 minutes**.

The initial version is `intraday-independent-initial-ta-v1`, implemented by
[starter_sizing.py](../../ml/intraday/starter_sizing.py) using
[technical_sizing.py](../../ml/intraday/technical_sizing.py). Each symbol has its
own versioned policy and frozen causal candle inputs. Shared initial periods
are explicit starter settings, not claimed symbol optimization or a score
computed once and copied across the watchlist:

- EMA20, SMA20 and VWAP20 distances normalized by that symbol's ATR14/price.
- Simple RSI14, Bollinger20 with two population standard deviations, and
  stochastic14 with three-bar K/D smoothing.
- Four-bar momentum normalized by twice ATR14/price.
- The issued not-down probability, oriented to the chosen BUY/SELL side.
- Relative volume versus the preceding twenty observed bars.

Each of the nine terms is normalized and clipped to `[-1, 1]`. The first seven
TA/direction terms and the probability term use BUY/SELL orientation; relative
volume is side-neutral. The score is `clip(0.5 + sum(terms)/18, 0, nextafter(1,0))`.
The original snapshot, reference, target, full recipe, term contributions,
equity/quote availability and result hashes are retained. First/second
differences are recorded as causal diagnostics but have no extra starter weight.
Windows count observed bars without manufacturing missing intervals. Zero ATR,
undefined flat-band/stochastic values, volume-free VWAP and insufficient history
produce an explicit unavailable/null size, not a fake zero.

Holdings, recent execution quantities and other contextual account inputs may
be logged or used by later versioned research recipes. They do not shrink the
approved starter's desired quantity. The first valid desired quantity and
explanation are immutable for that forecast. Capacity adjustment is a later,
separate step: desired BUY 4 at $50 with $100 actually available becomes BUY 2;
desired SELL 4 with three eligible shares becomes SELL 3. Zero adjusted shares
still preserve and score the forecast and original intention normally.

Future recipe tuning remains a separately identified experiment. It does not
reopen the approved starting configuration. Four quarter-hour trades may
cumulatively exceed one reference quantity.
Repeated sales may eventually close a position. There is no agreed automatic
window-expiry liquidation or forced end-of-day close.

## 4. Actual holdings and local order history

Each 15m trader may SELL actual same-symbol held shares after the broker's
pending SELL quantities, without a Gameplan horizon budget, donor policy or
allocation gate. A local log preserves the exact triggering forecast, broker
order ID, fill quantity, price and time. It does not invent an allocation or
unknown cost basis. Gameplan owns its own strategy and orders; no trader
cancels or rewrites another trader's instructions or private books.

A completed purchase remains a completed purchase after another independent
trader sells some stock. Current broker holdings determine what actually remains.
A balance change alone does not identify the other trader's individual trades,
and reconstructing that history is not a requirement for the next local action.

## 5. Broker-observed purchasing capacity

All three traders use the same real Schwab account and read its actual capacity
through their own existing connection. There are no separate cash budgets,
advance delegations, protected stream portions, shared reservation-service
requirements or mandatory inter-trader messages.

Save the complete desired size before observing the capacity adjustment. BUY
capacity is the broker's available cash already net of pending orders. SELL
capacity is actual held shares less pending SELLs. Round the affordable or
available quantity down to whole shares and use the smaller of that quantity
and the original intention. A desired BUY of four at $50 with $100 available
becomes two; a desired SELL of four with three held and two pending becomes one.
Zero capacity still leaves a complete forecast and intended-trade record.

Independent PCs can submit between another PC's account read and its POST.
This arrangement does not promise atomic cross-PC reservations. The broker's
actual acceptance/rejection is recorded, and each trader's own durable journal
prevents duplicate attempts. Proposed sales do not create spendable cash.
Unknown local sends remain fenced rather than being retried as new orders.

## 6. Independent logs and the only cross-PC planning flow

Every trader keeps its own forecasts, sizing explanations, adjusted quantities,
orders, fills, cancellations and actual outcomes locally. There is no required
exchange of execution logs, runtime proofs, account ledgers or order reservations
between traders during trading or after hours. Optional reviewed reporting does
not become a readiness, execution or nightly-completion gate.

Every night is already authorized by Jeremy's October 13 standing instruction:
Atlas produces its Gameplan, Scout produces its Gameplan, Scout synthesizes the
two plans, and Scout sends the result to Atlas. Delivery completes the handoff.
No further human permission, peer acceptance, third account snapshot or
cross-trader verification/reconciliation is an operating prerequisite. Each of
Gameplan, Atlas 15m and Scout 15m executes independently and keeps its own logs.
The first trader start remains Jeremy's manual action. Source publication and
private-data procedures continue separately from this operating authority.

## 7. Quote, execution, and recovery behavior

Fetch best bid, best ask, midpoint, spread, and current quote immediately before
or contemporaneously with submission. The initial Atlas path uses the ask for
BUY and bid for SELL as marketable limit prices, rounded to broker cents upward
for BUY and downward for SELL. Midpoint is recorded for
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

The initial accepted order remains working until it fills, reaches an actual
broker terminal status, or a newer same-symbol 15m forecast supersedes it after
confirmed cancellation. It does not inherit Gameplan's timeout or expiry
cancellation. No same-forecast automatic repricing timer is selected. Any later
same-forecast replacement must pursue only the confirmed unfilled remainder;
its cadence and price updates require a separately reviewed policy.

**Settled October 12:** a new 15m forecast replaces the preceding unfilled
intention for that symbol and 15m stream **after confirming cancellation/status**.
Save the new forecast and intention while reconciliation is pending. Do not
add a second independently reserved working order for the new forecast while
the preceding commitment is unresolved. Retrieve authoritative old-order status
and record intervening fills; a cancellation acknowledgment alone is insufficient.
Unknown submissions retain their identities and commitments throughout recovery.
An interrupted local attempt retains its durable one-send state before the
next own-order action. This rule does not cancel Gameplan orders or
another symbol's orders.

Reuse the existing Schwab cancellation and order-status machinery for both
Gameplan and 15m. The cancellation mechanism is shared; the reason and order
selection remain specific to each stream. A 15m new-forecast replacement must
not apply Gameplan's expiry rules or cancel another stream's order. Persist the
exact identity and cancellation request, reconcile authoritative status and
intervening fills, then release only the confirmed unfilled commitment. A
successful cancel request alone never proves cancellation. Source now fences
Gameplan's selectors to its own stream and supplies
[CancellationService](../../ml/intraday/cancellation.py) through the existing
prepared Schwab cancellation/status transport. Each trader selects only its own
orders; another trader's process, locks, installed code or status is not consulted
as an execution prerequisite.

Completed fills and the old forecast/intention remain in the audit history.
Repricing the same intention pursues only its unfilled remainder. A new forecast
has a newly evaluated symbol-specific intention from its original forecast
snapshot and the approved independent sizing recipe; actual capacity is applied
afterward. Do not copy the old quantity or automatically subtract old fills
from that newly computed quantity. The first desired quantity remains fixed
while cancellation or capacity observations change. Replacement also obeys
the submission cutoff below.

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

The manual worker starts from the latest eligible completed candle and skips
missed boundaries after an overrun, recording them rather than submitting a
backlog. Its ordinary-cycle admission requires a completed-candle boundary
after 04:00 and before 16:45 Pacific and a genuine same-session target interval.
This implemented admission bound does not establish the special opening or
overnight prediction contracts below. It never changes a forecast's fixed
target to compensate for acquisition or fitting delay.

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

The existing nightly tasks prepare local Gameplans and Stats using each PC's
own operating bindings. Scout performs the two-plan synthesis and sends the
completed result to Atlas every night under standing authorization. Atlas may
load the delivered result locally; an acceptance response is not part of the
handoff completion condition. The Gameplan trader uses its own actual broker
capacity at execution time, independently of either 15m trader.

Current operating references:

- [Nightly operations](../development/nightly-operations.md),
  [workflow](../development/nightly-workflow.md), and
  [private exchange](../development/nightly-exchange.md).
- [Scout manual execution](../development/scout-local-15m-manual-start.md) and
  [local submission contract](../development/intraday-scout-submission-contract.md).
- `ml/intraday/scout_local_execution.py`, `scout_books.py`, `scout_journal.py`,
  `scout_broker.py`, `live_session.py`, `starter_sizing.py` and `intentions.py`.

Earlier Atlas shared-authority and distributed-admission implementations remain
in source/history with their exact receipts. They are not the present trader or
nightly operating contract, and their old readiness gates must not be reintroduced.

## 12. Open decisions for subsequent development

1. Measured model/parameter/lookback comparisons beyond the implemented initial
   ten-model, all-compatible-history, full-15m configuration.
2. Separately versioned improvements to the approved independent TA starter;
   no improvement is silently substituted into an issued intention.
3. Optional local reporting improvements that do not create cross-trader
   dependencies or change the independent execution contract.
4. Any future same-forecast repricing policy. Initial orders remain working
   without a Gameplan timer until terminal status or a newer reconciled forecast.
5. Special final-information and next-session 04:00 targets, calculation timing
   and genuine observation availability; no overnight candles are invented.
6. Measured live publication offset, sustained resource feasibility alongside
   active Gameplan, and remote live delivery. The initial manual worker uses
   latest-completed/no-backlog overrun handling and requires a full ensemble;
   broader incomplete-ensemble execution remains a separate future choice.
7. An exact broker-supported recovery mechanism for an ambiguous POST that
   returns no reliable broker order ID. Current recovery preserves the unknown
   commitment and does not guess or resend.

Resolve only these genuinely open details; do not restart or reconfirm the
settled execution arrangement, moving not-down target, transparent first sizing,
broker-observed capacity, own-order recovery, or read-only UI requirement.

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
price leaves the interval unavailable. The live resolver requires the adjacent
preceding close within the same session: the prior completed 15m candle for a
leading omitted minute, or the preceding validated 1m constituent inside the
aggregate. An earlier session or an unacquired intervening interval cannot
supply that price. Every synthetic constituent retains its prior price/time
and continuity proof. Older stored synthetic observations without that proof
remain immutable but are excluded from new live input and outcome selection
pending audit; earlier issued forecasts and recorded scores are not rewritten.
Preserve source, interval, coverage,
request/retry evidence and the prior price reference. No fills across closed
sessions or nonexistent overnight targets are permitted.

An unavailable adjacent predecessor is a recoverable data prerequisite, not
permission to invent a price. The live runner retains the affected reference
for another bounded acquisition pass, refreshes verified same-source saved
observations, and requests the exact interval again. A predecessor learned
later cannot make an earlier response causally complete: confirmation must be
observed after that prerequisite is available. When covered repeated omissions
prove that a leading minute requires an absent predecessor, bounded recovery
may acquire that predecessor from the same source within the same session;
it cannot cross the session boundary or invent continuity. Provider waiting rules still
apply. A pending older reference does not prevent a newly eligible quarter-hour
reference from being attempted. Already issued symbol forecasts are reused
unchanged; recovered symbols retain their original reference and target and
receive their actual later issue times. Late forecasts remain scoreable but
cannot authorize orders after their fixed target. Once a newer quarter-hour
reference is being processed, recovery of an older reference is scoring-only
even if that older forecast's target is still in the future.
Current shadow/live cycle totals and 15m overrun flags include elapsed waits
between passes; cumulative attempt work and individual stages are recorded
separately, with an explicit measurement-scope version.

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

The common model runner, fixed-target forecast store, independent starter sizing
and intention projections retain their existing measured histories. The current
Scout path uses `ScoutLocalAccountExecutor`, its own execution lock, local
capacity permits and the existing prepared broker transport. Account identity
comes from the existing broker at manual startup, not Gameplan configuration.

Every full intended trade is saved before actual capacity can reduce it. Own
accepted order IDs, partial fills, cancellation and unknown submissions survive
manual restart. The trader never fabricates a fill, P&L, remote acknowledgment or
live result. Optional source diagnostics and offline tests do not call brokers,
start traders or impose a new operating acceptance ceremony.

The first real start is the human's manual command. Read its actual local status
and logs for live outcomes. Historical model timings, test receipts and earlier
Atlas runtime incidents remain in [the implementation record](../development/intraday-15m-prototype.md)
and [historical live observations](../development/intraday-live-readiness.md).
