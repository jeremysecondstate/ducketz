# Intraday 15-minute prediction and trading system

Design record: October 8, 2026; ensemble consolidation October 12, 2026.
Times in this document are America/Los_Angeles
(Pacific). Actor: Atlas. Scope: shared architecture for Atlas and Scout.

## Status and scope

Jeremy's October 13, 2026 standing direction defines three independent traders. Atlas runs the combined 22-symbol Gameplan trader and Atlas's local eleven-symbol 15-minute trader. Scout runs its own eleven-symbol 15-minute trader. Each trades and logs its own activity with its own local state. No trader checks, verifies, reconciles, combines, or depends on another trader.

Only the two nightly eleven-symbol Gameplans are synthesized. Use one actual account cash/stock balance, publish the same combined Gameplan and Stats on both PCs, and prepare the traders every night under permanent authorization. Atlas may synthesize directly and return the completed result to Scout without an acceptance wait. The human manually starts each trader. See [Independent trading systems](../development/independent-traders.md).

The causal forecast targets, model contracts, technical sizing and evaluation mathematics below remain in force. Existing authorized data/provider connections remain local. Normal preparation and routine repair do not require new human or peer approval.

## 1. Three execution streams, one account

| Stream | Execution PC | Symbols | Forecast source |
|---|---|---|---|
| Existing Gameplan | Atlas | Combined 22 symbols | Nightly 1h, 4h, 1d, 1w Gameplan |
| Atlas 15m | Atlas | Atlas's eleven | Fresh local intraday predictions |
| Scout 15m | Scout | Scout's eleven | Fresh local intraday predictions |

All three use the same Schwab account. Each PC locally fetches, computes,
predicts, sizes, logs, and organizes its own intraday data in its local DATASTORE.
The human manually starts Atlas's intraday trader. Startup selects the latest
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

## 4. Actual share ownership without a 15m horizon funding gate

Each 15-minute trader sizes from its own forecast and current technical inputs. Actual broker cash, available shares and working orders bound the submitted quantity. A proposed SELL can use currently available same-symbol stock without consulting Gameplan's horizon allocations or another trader's records.

Own order IDs, fills and unresolved submissions stay in the local 15-minute ledger. Account movements caused elsewhere are ordinary changes in available capacity; do not reconstruct their cause or turn them into a cross-trader discrepancy. Preserve original fills, and never repeat a completed purchase to restore an earlier account balance.

## 5. Actual account capacity and independent working quantities

The broker account is shared; the traders' operating state is independent. Each worker reads actual broker cash/positions and applies its own pending-order commitments. There is no shared reservation book, Gameplan allocation attribution, cross-PC cash grant, distributed lease or per-trader allowance exchange.

Gameplan records its own activity in `state/gameplan-trader/holdings.sqlite3` and `state/gameplan-trader/authority.sqlite3`. Each PC's local 15-minute trader uses `intraday/execution/holdings.sqlite3`, `intraday/execution/authority.sqlite3` and its own local execution binding/lock. Preserve old mixed-ledger files as historical originals.

A trader wanting to sell four shares when the broker has three available submits at most three. A trader wanting to buy four shares at $50 with $100 usable cash submits at most two. Save the original desired quantity and actual adjusted quantity, including zero; never fabricate a fill.

## 6. Independent activity records and nightly Gameplan exchange

There is no live or overnight reconciliation between the three traders. Each records its own forecasts, order identities, cancellations, fills and outcomes. Optional telemetry is informational and cannot block operation.

The private nightly exchange carries the two Gameplan research packages, their Stats and the single combined Gameplan result. Both PCs receive that same result. It is not an exchange of 15-minute trading ledgers, reservations or verification receipts.

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
An unsent reservation also requires a confirmed release before reserving or
submitting the new intention. This rule does not cancel Gameplan orders or
another symbol's orders.

Each trader cancels or recovers only its own exact broker orders. No loaded Gameplan proof or other-trader absence check is required.

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

The current nightly workflow is permanently authorized: local completed-session data and Stats, local model work, each PC's eleven-symbol Gameplan, one combined 22-symbol cash/stock synthesis, and the same result returned to both PCs. Atlas can finish synthesis directly. Normal preparation leaves all three independent traders ready for manual start.

Current operating references are [nightly workflow](../development/nightly-workflow.md), [private nightly exchange](../development/nightly-exchange.md), [Gameplan composition](../loops-system-analysis/JOINT_ACCOUNT_CAPITAL.md), and [independent traders](../development/independent-traders.md). `Start-Gameplan-Trader.cmd` belongs to Atlas's combined Gameplan; each PC has its own `Start-15m-Trader.cmd`.

## 12. Open decisions for subsequent development

Future research may compare model parameters, lookbacks, training cadence, technical recipes, measured publication offsets and sustained resource use. Special final-information and next-session 04:00 targets remain separate forecast contracts; no overnight candles are invented. A future same-forecast repricing policy must preserve original intention/order identities.

The independent-trader topology, lack of cross-trader verification, single Gameplan synthesis and permanent nightly authorization are settled. A distributed reservation protocol or a compatible Gameplan process is not required for Scout or Atlas 15-minute execution.

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

The [implementation record](../development/intraday-15m-prototype.md) preserves model/data contracts and dated research measurements. The [manual execution guide](../development/intraday-live-readiness.md) describes current independent state and launch commands.

`ml/intraday/local_execution.py` supplies the local execution binding and own-state paths. `account_execution.py` connects independent sizing and actual broker capacity to the prepared broker transport. `live_session.py` retains the local manual session, own-order retries, fixed forecast targets, current quotes and strict cutoff. Gameplan and other 15-minute runtime state are not inputs.

`Check-15m-Readiness.cmd` is an optional local diagnostic. `Start-15m-Trader.cmd` is the human's local manual launcher. Source publication and nightly preparation do not start trading. Retain historical measurements as dated evidence; they do not impose the former shared-ledger or compatibility architecture.
