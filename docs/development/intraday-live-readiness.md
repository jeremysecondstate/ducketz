# Independent 15m operation and historical live evidence

Times use America/Los_Angeles. The [current design](../15m-trading-system/INTRADAY_15M_SYSTEM_DESIGN.md)
and [Scout local runbook](scout-local-15m-manual-start.md) govern current operation.

Every night is already authorized by Jeremy's October 13 standing instruction:
Atlas produces its Gameplan, Scout produces its Gameplan, Scout synthesizes the
two plans, and Scout sends the result to Atlas. Delivery completes the handoff.
No further human permission, peer acceptance, third account snapshot or
cross-trader verification/reconciliation is an operating prerequisite. Each of
Gameplan, Atlas 15m and Scout 15m executes independently and keeps its own logs.
The first trader start remains Jeremy's manual action. Source publication and
private-data procedures continue separately from this operating authority.

## Manual start and optional local diagnostics

Scout's real manual command is:

```powershell
.\.venv\Scripts\python.exe -B -m ml.intraday.live_session --live --scout-admission broker-local
```

`python -B -m ml.intraday.scout_prestart` is an optional local diagnostic. It
neither accesses the broker nor starts a worker; its report is not an approval
ceremony. The actual manual session uses the existing broker account directly.
No Gameplan config, Atlas address/certificate, loaded-writer proof, peer response,
shared lock or other-trader reconciliation is needed. Keep existing manual
start/stop controls; do not restart another trader during preparation.

The local forecasting, full-ensemble, fixed-target, quote, account-identity,
market-window and 16:45 Pacific cutoff rules remain in force. Save full intended
sizes even when actual available cash/shares reduces an order to zero. There are
no Gameplan horizon budgets or protected cash portions in 15m sizing.

## Own orders and local logs

A durable local permit precedes Scout's single send. Acceptance requires the
actual broker order ID; a lost response is not permission to retry. The local
journal retains every original session's own status, partial fills, cancellation
and terminal evidence. Replacing an own order waits for exact terminal status,
not another trader's receipt. Local logs never fabricate allocations, fees or P&L.
The watch reads these saved observations; its registration is not a startup gate.

## Historical implementation evidence

The records below preserve actual October 12 observations and exact earlier
source/repair receipts. They describe the then-current shared-book implementation.
They do not restore its Gameplan/shared-lease prerequisites or require a replay,
new cross-trader verification, or peer acceptance under the October 13 policy.

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

## Ready snapshot timing and delayed predecessor recovery

Following the human reloads, Gameplan recovered normally from observed shared
lock contention. It later reported `Pending buy exposure has no usable current
quote`, temporarily becoming degraded and blocking 15m execution through the
existing compatibility gate. It subsequently recovered on its own. The saved
capture contained admitted prices for all configured symbols, but not the raw
per-quote timestamps; the exact live offending time difference is unavailable.

An offline reproduction identifies a concrete timing defect: a valid pending
BUY quote timestamp can occur during a request, later than the snapshot's
request-start timestamp. The repaired Gameplan adapter checks original capture
age, completes account identity verification, and records actual information
readiness. Its audit envelope retains request start, original observed time,
original fingerprint and validation time. Quotes retain their own timestamps.
Existing future, stale, missing, crossed-price and identity guards remain.

The restarted 15m session's first pass saved eight complete ten-model ensembles
in 300.682 seconds. Three symbols failed before training because their reference
needed an adjacent verified predecessor for no-trade constituents. Same-source
predecessor observations arrived later. Strict continuity refusal was correct;
abandoning recovery for that reference was the defect.

The live recovery loop now keeps pending references, performs bounded passes,
and refreshes causally available same-source predecessor evidence before a new
exact-interval response. Covered repeated leading omissions may trigger a
bounded request for an absent same-source predecessor inside the same session.
Newly due quarter-hour work takes priority. Durable
cycle evidence restores pending work within the same manual session, with no
retry-count ceiling. Already issued symbols are not overwritten or retrained.
Recovered forecasts retain their original targets and actual issue times;
late predictions are saved and scored but remain ineligible for live orders.
Recovery of an older quarter is also scoring-only once newer quarter-hour
work has begun, even while the older target is still in the future.
Shadow/live cycle totals and overrun flags count wall time including waits
between passes; cumulative attempt work remains separately measured. A long
provider wait cannot be presented as a cycle that met the 15m interval merely
because fitting itself was fast.

Verification uses injected offline clocks, providers and brokers. No diagnostic
provider/broker requests, extra training runs, order actions or control changes
were made for this repair. That repair was developed and reviewed in isolation while
workers ran. Its original installation plan required both affected workers to
be absent and described a Gameplan reload before 15m. Those historical conditions
and the then-shadow-only Scout status are superseded by the October 13 independent
trader instruction. The recorded historical source/results remain unchanged.
