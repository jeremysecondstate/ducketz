# Atlas intraday 15m offline prototype

This first build implements the settled structural behavior from the
[saved design, PR #28](https://github.com/jeremysecondstate/ducketz/pull/28),
source commit `9d4ca66858d148cdbeaeee46e4031428ca6d15d1`. Its authoritative
local copy is `docs/loops-system-analysis/INTRADAY_15M_SYSTEM_DESIGN.md`.
This implementation does not revise the design's open decisions.

Actor: Atlas. Producer: human-authorized-development. Task:
intraday-15m-offline-prototype. Shared source is applicable to both PCs;
the implemented execution scope is Atlas first. Source publication, main
integration, local installation and loaded runtime deployment are distinct.

## Components and offline verification

`ml/intraday` is a separate package using the standard library. It does not
import the concurrently edited stock-trader contracts, account configuration,
Gameplan runtime or existing reservation authority. The current Gameplan's
combined 22-symbol nightly stream and four horizons retain their operating
bindings. No launcher, schedule or production entrypoint is added.

| Component | Implemented behavior |
|---|---|
| `prediction.py` | Opening-time completed candles, actual availability, immutable quarter-hour issues, exact not-down targets and append-only outcome evidence. |
| `sizing.py` | Transparent hourly-reference multiplication and nearest whole shares; explicit reference/score versions, tie rule and rounded-equality handling. |
| `book.py` | Host-local offline account book with atomic reservations, exact identities, allocation ownership, fill attribution, account/exposure headrooms and durable history. |
| `execution.py` | Explicit mock venue seam, contemporaneous quotes, ask-BUY/bid-SELL proposal, unknown-submission recovery, status retries and cancellation reconciliation. |
| `prototype.py` | Read-only Atlas symbol binding checks, a full local issue cycle, partial-cycle retry, frozen sizing explanation and an instruction bound to the original issue. |

Run the focused offline checks using the project's Python environment:

```text
python -B -m pytest tests/test_intraday_prediction.py tests/test_intraday_book.py tests/test_intraday_execution.py -q -p no:cacheprovider
```

Tests use temporary databases, explicit model/score/venue fixtures and injected
clocks/sleep functions. They exercise the eleven-symbol Atlas issue workload,
refreshes, end-to-end issue/size/quote/reserve/fill/outcome behavior, delayed and
future observations, conflicting data, exact targets and equality labels,
immutable issues, outcome corrections, duplicate prevention, simultaneous local
commitments, matched working orders, filled-BUY accounting, adjusted sales,
hierarchy, allocation attribution, cancellation races, replacement cost and
remainders, send-time crashes, unknown submissions and more than six sends.

## Data and prediction contract

For an issue shortly after 10:15 Pacific, the latest available completed candle
must open at 10:00. The reference is its OPEN; the target candle opens at 10:30
and closes at 10:45. The model declares
`intraday-15m-open-to-third-close-not-down-v1`; a legacy strictly-positive model
is rejected. The implementation records the exact input snapshot and hash,
availability and issue times, data-through boundary, reference and target,
probability and model version. It does not select a model or train one.

Each symbol/reference slot has one immutable issued prediction. A partial
Atlas cycle retries missing symbols and reuses completed issues. A new
quarter-hour produces new issues. Outcome records identify genuine target
candles and distinguish completed, pending and missing observations. Equality
is not-down. Later data corrections append evidence without rewriting an issue
or earlier observation. No probability classification threshold or final
accuracy metric is selected; no provisional observations are counted as final.

The caller supplies an eligible session date from a future calendar adapter;
this package does not infer market holidays. It leaves the special 04:00
next-open and final 16:45 prediction contracts unresolved. Final fetched
information can be saved immutably through `save_final_information`, with no
fabricated target or overnight inference. This preserves the future immediate
04:00 prediction requirement without substituting an intraday gap model.

The initial indicator seam uses 23 genuine contiguous completed candles for
three causal measurements: one-step momentum, 14-change simple RSI, 20-candle
typical-price volume-weighted average and simple moving average, 14-candle
simple ATR relative to price, and relative volume against a supplied causal
baseline. It records rates and second differences per 15m step. These are
explicit prototype indicator definitions, not a selected trained model or
score recipe. Missing early-session history remains a visible data gap.
Holdings, recent executed quantities and volume baseline carry availability
times; future inputs are rejected. Sizing also receives the preserved forecast
probability and must use its original candle inputs.

## Policy seams retained explicitly

The sizing caller supplies a positive normal hourly reference even when no
hourly order is due, and a versioned score in `[0, 1)`. Raw quantities therefore
remain below hourly size. A non-tie rounds to the nearest share. Half-share
ties require an explicit `half_even` or `half_up` fixture rule. If rounding
equals hourly size, explicit `rounded_equality="allow"` is available for an
offline fixture; omission reports the unresolved policy. Neither option is
selected for live use. There is no flooring substitution, minimum-trade veto,
hourly aggregate cap, expiry liquidation or forced end-of-day close.

SELL requests carry specific allocation identities and explicit admitted
quantity ceilings in the settled 15m/1h/4h/1d/1w hierarchy. Capacity adjusts
the desired quantity to eligible, uncommitted shares. Longer signals cannot
sell shorter allocations. Access to longer allocations requires a versioned
external sale-policy seam; tests label it as an offline fixture. The mechanism
can test spanning allocations, but does not select permission to span, daily
ceilings or pacing for production.

The reviewed legacy bearish-sale rules remain in the existing source: fallback
only when own inventory is genuinely absent, at most one donor per forecast,
50% shared daily and allocation ceilings, 18 opportunities/24 weight units,
simultaneous-instruction exclusions and pending-order restrictions. Those rules
need a prospective 15m policy decision. They are neither copied unchanged into
the prototype nor removed from Gameplan.

Offline admissions carry an explicit identity supplied by the caller. The
SQLite transaction serializes mutations to prevent double commitments; it
does not establish an agreed arrival-order priority. Contention priority and
distributed communication remain open. Scout execution is rejected, and no
network service or shared-drive SQLite protocol is implemented.

## Accounting and recovery

One offline book can receive Atlas 15m and mock Gameplan requests from the
same purchasing pool. The existing active Gameplan authority is not connected
to it. Production connection requires an explicit reconciliation/migration
adapter after the trading policies are resolved. The database must be called
`offline-intraday.sqlite3` and must use a separate host-local fixture directory;
tests never open active ledgers or copy account state into source publication.

Capacity evidence supplies cash, gross and symbol headrooms already net of
unmatched broker commitments and the applicable account constraints. Exact
identified working orders overlap local reservations only once. BUY fills
remain charged until account evidence explicitly identifies their executions;
SELL fills do not create purchasing capacity. Balances alone report inventory
discrepancies without manufacturing fills or allocation effects.

Requests retain account, stream, forecast, symbol, side, desired and permitted
quantities, price, allocation sources, sizing explanation and persistent
identity. Each fill preserves its execution identity/price/time and derived
allocation effects. A completed ten-share hourly purchase remains completed
after a 15m sale reduces its remaining allocation to seven; retries do not
replenish it.

The mock executor persists the submission gate before sending. A crash,
timeout or ambiguous send retains commitments and looks up the same request
identity. A nonauthoritative missing status never releases capacity or permits
another POST. Temporary quote/status failures retry without a count ceiling,
sleeping at least a few seconds and respecting a supplied retry-after interval.
Invalid requests and code problems propagate for correction while ambiguous
submissions retain their commitments. Stopping a test preserves unknown state.

Quotes record bid, ask, midpoint, spread, current price and observation/receipt
times. Quote age tolerance is an explicit caller input. An unchanged fresh
quote is usable; an unavailable/stale quote retries. Spread width adds no
execution veto. Order evidence records actual acceptance/status and identified
fills; a successful send is never treated as a completed trade.

Cancellation acknowledgment does not free capacity. Complete old-order status
and intervening fills must establish cancellation before a caller explicitly
requests a replacement. Only its permitted unfilled remainder may be reserved;
an increased BUY price must fit remaining account/exposure capacity. The
prototype does not automatically select repricing cadence, cancel/resubmit
timing or interaction with a new prediction. An unsent persisted reservation
needing repricing stays reserved and reports that unresolved policy.

## Next dependent work and deferred UI

Before automatic trading quantities, resolve BUY/SELL hourly reference sizes,
score mapping, half-share ties and rounded equality. Also retain the model and
retraining choice, 15m donor-sale restrictions, contention policy, automatic
repricing/new-prediction behavior, eligible-calendar and session-end/next-open
targets as explicit open decisions.

Real acquisition/inference timing across the whole workload alongside Gameplan,
and retraining if selected, require operational authorization. This offline
verification establishes behavior with fixtures, not measured production
latency, predictive skill, broker mechanics or runtime readiness. It makes no
provider/broker call, live order, training run, application start/restart,
schedule change, deployment or merge.

Preserve design section 10's requested dedicated 15m activities/accuracy UI tab
with approximately quarter-hour refreshes. The tab remains deferred. Its later
view must distinguish completed, pending, missing and explicitly provisional
outcomes and show received-data freshness; an order adjustment must never
rewrite the original forecast or imply a remote update arrived.
