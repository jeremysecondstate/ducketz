# Atlas 15m manual execution and readiness

Actor: Atlas. Shared engineering record, October 12, 2026. Times are
America/Los_Angeles. Read the [consolidated design](../15m-trading-system/INTRADAY_15M_SYSTEM_DESIGN.md)
and [forecasting implementation record](intraday-15m-prototype.md) together with
this record. Existing manual Gameplan controls and ownership remain in force.

## Source and operating boundary

Source now implements an explicit human-started Atlas session, genuine full
ensemble forecasting, independent approved TA sizing, durable intentions,
actual shared-account capacity admission and exact-order recovery. Verification
uses injected brokers and temporary native/authority databases. This change did
not start a live session, submit orders, migrate operating databases, change
schedules or restart/deploy Gameplan. Source publication, local installation,
compatible loaded workers, human launch and observed live outcomes are distinct.
Scout retains research/shadow operation until later human activation and an
implemented distributed reservation protocol.

## Commands and real readiness gates

From the reviewed local Ducketz checkout, run the diagnostic:

```powershell
& '.\Check-15m-Readiness.cmd'
```

It performs read-only source, local binding, saved evidence and runtime checks.
It does not call providers/brokers, start workers, submit/cancel orders or change
account state. `live_execution_implemented` describes parsed source interfaces
and wiring. `manual_live_source_ready` describes the inspected source/binding/
runtime/cutoff prerequisites. A passing diagnostic reports
`MANUAL_LIVE_PREFLIGHT_REQUIRED`; blockers produce `MANUAL_LIVE_START_BLOCKED`.
`manual_live_start_ready` remains false and `manual_live_preflight_required`
remains true because this diagnostic cannot establish fresh broker/account/
quote evidence. Saved full-current forecasts are descriptive evidence, not
permission to execute old issues. Keep the diagnostic and private facts local.

The explicit human manual launcher is:

```powershell
& '.\Start-15m-Trader.cmd'
```

Equivalent CLI, using this PC's existing paths rather than copied bindings:

```text
python -B -m ml.intraday.live_session --live --repository LOCAL_REPOSITORY --datastore LOCAL_DATASTORE --profile LOCAL_PROFILE --watchlist LOCAL_WATCHLIST
```

That command can submit real orders when the runtime gates pass. It installs no
task or schedule and does not start/restart Gameplan. Without `--live`, the
module only reports readiness. The manual worker uses existing authenticated
Schwab and EQUS.MINI provider bindings; no new credentials, subscription or
spending authority is supplied by this source change.

Startup chooses the latest eligible completed reference on the quarter-hour
grid plus the configurable publication offset. Thirty seconds remains a
candidate offset, not measured provider publication latency. Exact completed
data must still validate. The worker fully fits all ten enabled models per
symbol, publishes each symbol when complete and records actual issue time with
its original future target. It skips and logs missed boundaries after overruns;
it does not trade a backlog or use old fitted models as a fallback.

Before each account operation, and again before POST/DELETE, the source checks
manual-session authority, current local/account identities, the shared lease
and the compatible Gameplan state. Entry additionally requires a newly issued
current-session Atlas live forecast with observed availability, full configured
ensemble, fixed unexpired target, a genuine executable market window, fresh
account/quote evidence and time strictly before 16:45 Pacific. Old shadow/replay
forecasts, incomplete ensembles and forecasts predating manual start cannot
submit. A late fit cannot move its target or backdate an issue.

[runtime_compatibility.py](../../ml/intraday/runtime_compatibility.py) requires
either a compatible loaded Gameplan attestation matching its living process,
process birth, lock, heartbeat and reviewed code fingerprints, or independently
verified complete Gameplan-worker absence plus installed capability checks.
Missing/stale status is not absence evidence. An older running worker without
the required proof blocks admission even if newer source is on disk. The
diagnostic never restarts it; the human's existing manual controls remain
separate from source installation.

## Approved independent sizing

[starter_sizing.py](../../ml/intraday/starter_sizing.py) implements the human's
October 12 starter configuration, version `intraday-independent-initial-ta-v1`:

`reference shares = 0.015 * current equity / fresh ask-or-bid`

`desired shares = half_up_nearest(reference shares * score)`

BUY uses ask and SELL uses bid. Nine own-symbol causal terms cover EMA20,
SMA20, VWAP20, simple RSI14, Bollinger20, stochastic14, momentum4, issued
probability and prior20 relative volume. Distances/momentum use that symbol's
ATR14 normalization. Clipped terms produce
`clip(0.5 + sum/18, 0, nextafter(1,0))`. The
[complete recipe](../15m-trading-system/INTRADAY_15M_SYSTEM_DESIGN.md#3-approved-independent-technical-sizing)
defines periods, side orientation and undefined-input handling. Shared starter
periods do not imply an optimized common score; each symbol has its own frozen
inputs, measurements, contributions and versioned policy.

This replaces the older hourly-reference dependency. No due Gameplan order,
horizon allocation, protected cash portion, stream budget or reserve buffer
determines desired size. Half-share ties round upward and rounded equality is
allowed. Missing/undefined TA, equity or quote evidence yields an explicit null
size; a genuinely computed zero remains zero. First valid desired size and full
evidence are frozen for that forecast in
[IntentionStore](../../ml/intraday/intentions.py).

Actual capacity adjustment is separate. Desired BUY 4 at $50 with $100
available becomes BUY 2; desired SELL 4 with three eligible uncommitted shares
becomes SELL 3. Zero capacity saves desired and adjusted quantities plus the
concrete disposition. Every forecast remains independently scoreable. No
submission produces a fabricated fill or realized P&L.

## One existing account pool and native ownership ledger

[account_execution.py](../../ml/intraday/account_execution.py) uses the existing
host-local `state/account-gameplan/authority.sqlite3` and
`state/independent-stock-trader/holdings.sqlite3` beneath local DATASTORE. It
does not create an independent cash book. Model training holds neither the
shared account lease nor the existing Gameplan cycle lock. Bounded broker steps
serialize reconciliation, fresh account reads, native/shared admission and
the final submission gate under that lock; retries return to the outer loop.

The authority's explicit intraday snapshot/admission path uses actual available
cash and eligible shares, counting all stream commitments once by exact order
identity. Gameplan daily generations, risk budgets, 95% reserve fraction and
horizon weights remain its own policies; none is a 15m admission prerequisite.
Broker cash is already net of its identified pending reservations. Local
unrepresented commitments and fills not yet proven in newer balances remain
charged without double subtraction. Proposed SELLs create no spendable cash.

15m sales can span actual same-symbol native lots and observed unallocated
stock. Stable allocation-identity slice order is accounting, not a donor gate,
daily ceiling or pacing policy. Each fill retains the triggering 15m forecast,
exact broker identity and actual donor reductions. Completed Gameplan purchases
remain completed after stock is sold; no replenishment order is fabricated.
Compatible Gameplan selectors remain fenced to their own reservations and
inventory, preserving its selected expiry and longer-horizon policies.

## Accepted orders, cancellation and recovery

An accepted 15m order remains working until it fills, reaches broker terminal
status or a newer same-symbol 15m forecast supersedes it. It has no Gameplan
timeout/expiry policy and no automatic same-forecast repricing timer. A new
forecast is independently sized and saved even when its desired quantity is
zero. The preceding unfilled intention must reconcile before the new one can
reserve or submit. Older processed references cannot revive after a newer
zero-sized intention.

[CancellationService](../../ml/intraday/cancellation.py) reuses
`SchwabSession.cancel_prepared_order` and the existing exact-ID
[order evidence reader](../../ml/stock_trader/horizon_broker.py), with explicit
stream/reason selection and final identity/authority gates. It never applies
Gameplan cancellation reasons to 15m or cancels Gameplan's order for a 15m
replacement. Persisted cancellation request/acknowledgment survives interruption.
A DELETE acknowledgment does not establish terminal cancellation or release
capacity; complete status and intervening fills do. Only the confirmed
unfilled commitment is released, preserving previously filled quantities/costs.

The submission context is prepared before POST. Shared/native reservations
retain forecast/snapshot hashes and exact identities. Acceptance returned in
the broker `Location` is journaled as immutable authority evidence before final
status projection, including when a slow response outlives its lease. Evidence
recording does not release capacity or bypass fencing. A new valid lease promotes
that exact receipt; Atlas's 15m recovery then repairs its native identity before
status capture, without another POST. This also repairs an interrupted native
write. The older Gameplan bridge still captures native evidence before acquiring
authority; automatic native repair for its own unknown-ID case is outside this
15m adapter and remains a separate integration limitation.
Provably never-sent native reservations can be released. A definitively
`NOT_SUBMITTED` attempt can be linked to a new fresh-quote/capacity attempt while
preserving the original desired size and all earlier evidence. Temporary
read/status failures retain sleep-and-retry waits, including provider-required
waits, without an arbitrary retry-count ceiling.

An audited `SubmissionNotStarted` before the final send gate also receives
immutable no-send evidence if its lease expired during a slow pre-submit read.
The native attempt stays reserved until a fresh authority lease promotes that
proof to `NOT_SUBMITTED`; only then can native recovery release it and create
a linked retry. Generic exceptions and failures after the send gate are never
treated as proof that no order reached the broker.

An ambiguous POST without a reliable order ID stays UNKNOWN/SUBMITTING and
committed. Neither bounded missing history nor matching symbol/quantity/price/
time proves nonacceptance. The exact-ID reader cannot invent an identity;
automatic broker-supported recovery of such an unknown ID remains a technical
limitation. No unknown attempt is resent or guessed away.

Forecasting, durable execution queue/retry records and bounded target-scoring
passes are separate. Each due outcome receives at most one request per scoring
pass; failed acquisition retains its own persistent interval wait and later
passes retry. Forecasts and intention history remain immutable through late
data and outcome revisions. At or after 16:45:00 Pacific no new POST is allowed,
including replacement attempts, while status/fill/scoring work continues.
Ctrl-C immediately withdraws this manual session's order authority and reports
`STOPPING` while active model work drains. Queued symbols skip acquisition and
training after the stop. The session lock remains held until its workers exit;
resting orders and their identities remain at the broker. Startup or unexpected
worker failures report `FAILED`, with a nonzero exit; normal manual shutdown
reports `STOPPED`. These controls do not stop Gameplan.

## Verification and remaining work

Focused offline checks use real temporary native/shared/forecast databases and
injected broker/account/quote/status observations. They cover shared capacity,
smaller/zero BUY and SELL quantities, independent sizing without Gameplan plans,
immutable desired evidence, stream isolation, current-session admission,
new-zero supersession, partial-fill cancellation, final cutoff/manual-stop/
identity gates, unknown acceptance, accepted-ID crash repair and durable retry.
Legacy fixtures remain available for their historical contracts; they do not
override these later policies. Exact final-source results accompany the source
handoff rather than being inferred from earlier test counts.

No live fitting/acquisition/acceptance timing or completed real trade was
measured for this change. Prior October 12 research timings are retained in the
[prototype record](intraday-15m-prototype.md#measured-atlas-research-runs-october-12-2026).
Active-session resource contention and a useful live publication offset still
need measurement after an authorized human launch. Realized P&L is not inferred
from intentions or fills without complete accounting.

The final-information and next-eligible-session 04:00 reference/target and
calculation timing remain unresolved. Ordinary manual admission bounds are an
implemented provisional boundary, not an overnight-gap model. Do not create
closed-session candles or move targets to another date. Future TA tuning and
same-forecast repricing require separately versioned review. Scout order
submission still needs the distributed Atlas reservation protocol and later
human activation; read-only telemetry or shared SQLite copies cannot supply it.

## First manual session and shared-lock recovery, October 12

The human restarted Gameplan at 09:55 Pacific and manually started Atlas 15m
at 09:56. Read-only evidence confirmed the new Gameplan loaded-code proof.
The first 15m cycle freshly fitted all 110 configured models and persisted all
eleven symbol forecasts in 187.963 seconds, excluding the initial local-history
startup load. Its reference was 09:30 and its unchanged target close was 10:15.
This is measured forecasting-cycle latency, not time to complete all orders or
evidence of predictive quality.

At 09:57:54, Gameplan encountered the shared `stock-trader-hourly.lock` while
15m held it for a bounded account operation. The session treated that ordinary
contention as an unhandled runtime error and exited. Consequently this sample
does not establish sustained full-workload performance alongside active
Gameplan. Private order/fill evidence and original failure logs stay local.

The session-layer repair recognizes only the actual lock generator's
unacquired ownership refusal, with its original `FileExistsError` cause and
the exact shared-lock path. Matching exception text alone is insufficient.
It persists a separate `runtime_lock_wait`, sleeps five seconds, and returns
to the normal activation, date and session-close checks without a retry-count
ceiling. Successful acquisition clears the wait. Pending entry eligibility is
retained only within the same hour and quote-recovery intent is not consumed
by a lock refusal. Unrelated exceptions still fail visibly.

Waiting preserves the previous completed cycle, its genuine loaded-code
attestation and existing failure counters. It does not fabricate a completed
cycle, fresh broker evidence or a proof before the first completed cycle. The
lock remains owned by the other process; no stale-lock override is introduced.
The installed console formatter from the earlier reviewed reporting change is
retained. Only a stopped Gameplan session entrypoint is eligible for local
installation of this repair. Active 15m-imported code, trading controls,
schedules, ledgers and resting orders are unchanged. A later human Gameplan
relaunch and observed lock contention are separate runtime verification steps.

## Broker execution timestamp precision and observed availability

The first manual run also exposed an exact-ID reconciliation boundary: an
original broker execution timestamp had whole-second precision, while the local
reservation used microseconds. The normalized fill appeared 28.841 milliseconds
before that reservation. The price and observation-time checks passed. A bounded,
read-only request through the existing authorized account connection confirmed
the original wire timestamp; no order was submitted or cancelled by that diagnostic.

The repair retains the original raw timestamp and its selected execution-leg or
activity field in versioned `fill_time_provenance` on new order evidence. The
original executed time, fill identity, quantity and price remain unchanged. The
native and account ledgers append the new observation; existing evidence and the
four-field fill records are not rewritten. Evidence without this provenance keeps
the original strict time check. A raw fractional timestamp, including `.000`, is
not inferred to have whole-second precision merely because normalization removes
trailing zeros.

Only a verified original whole-second timestamp can represent the interval from
that second up to, but excluding, the next second. That interval must overlap the
reservation's creation time. An earlier second, a timestamp after observation,
a changed fill, mismatched identity, unbalanced quantities or a price outside the
order limit still fails. Both the Gameplan and 15m account adapters carry the
provenance, and loaded-code compatibility includes the new validation path. This
is a precision rule, with no configurable clock-skew tolerance.

These shared execution files require a coherent manual reload. Publish and test
first; verify both affected workers are stopped before local installation. The
human then starts Gameplan, waits for genuine loaded-code evidence, and starts
15m. Resting broker orders keep their identities and are reconciled on recovery;
a stopped process does not imply that its orders were cancelled. No live database
patch, replacement forecast or resubmission is used to repair accounting.

Observed data latency is also material: the required 10:00-10:15 Pacific interval
was outside the provider's advertised schema coverage until approximately 10:21.
The first saved complete response arrived at 10:21:15, 6 minutes 15 seconds after
candle close. All eleven first-cycle outcomes subsequently scored through normal
recovery: seven observed and four with explicitly synthetic constituents. This
is evidence of acquisition/scoring recovery, not predictive accuracy. The next
forecasting cycle resumed without weakening no-trade confirmation. The candidate
30-second acquisition offset is therefore not proof that data is already ready.
