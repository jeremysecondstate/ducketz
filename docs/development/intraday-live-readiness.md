# Independent 15-minute manual execution

Jeremy's October 13, 2026 direction permanently authorizes this nightly workflow. Atlas and Scout each produce Gameplans for their own eleven symbols. Synthesize those two Gameplans once, using one actual cash and stock balance, into the same 22-symbol Gameplan shown on both PCs. Atlas can perform synthesis directly and return the completed result to Scout without a reciprocal acceptance wait.

Atlas runs the combined Gameplan trader and its own 15-minute trader. Scout runs its own 15-minute trader. All three trade and log independently. There is no 15-minute synthesis, cross-trader reconciliation, compatibility verification, shared trading ledger, distributed grant, or approval prerequisite. Preparing the plans and traders is permanently authorized; the human manually starts each trader.

The current operating contract is [Independent trading systems](independent-traders.md).

## Manual launch and own state

`Start-15m-Trader.cmd` starts this PC's own 15-minute trader only when the human invokes it. `Check-15m-Readiness.cmd` is an optional local diagnostic, not a cross-trader verification or approval stage. A configured local launcher reports `READY_FOR_MANUAL_START`; this requires no preflight approval stage. Neither launcher reads another trader's session, ledger, process, source attestation or acceptance status.

The local execution binding is `intraday/execution/config.json`; the local holdings and reservation databases are `intraday/execution/holdings.sqlite3` and `intraday/execution/authority.sqlite3`. The local lock is separate from Gameplan. Preserve historical mixed state and receipts without importing other traders' internal history into the new epoch.

Each worker fits and publishes its own causal forecasts, sizes them independently and uses actual broker cash/shares and quotes to bound its own order. The approved size is `0.015 * current equity / fresh side price * own-symbol TA score`, nearest-rounded half-up. The nine-term score, target and full-ensemble contracts remain in the [design](../15m-trading-system/INTRADAY_15M_SYSTEM_DESIGN.md).

## Own orders and recovery

Record the true local order identity and fills. New same-symbol forecasts supersede this trader's own prior unfilled intention after its own cancellation/status response. Unknown sends remain committed until their own result is known. Do not cancel another trader's orders or infer its fills from account movements. Broker cash/positions are account capacity; they are not a reconciliation of trader histories.

The existing market window, strict pre-16:45 Pacific submission cutoff, actual quote timing and no-backlog forecast policy remain. The human starts each worker separately; permanent nightly preparation does not turn trading on.

## Archived October 12 observations

The following dated measurements preserve the original incident record. They describe the former coupled implementation and are not current operating instructions. Shared locks, Gameplan compatibility proofs and cross-trader reconciliation described in that history were removed by the October 13 independent-trader direction.

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
were made for this repair. It is developed and reviewed in isolation while
workers run. Both affected workers must be absent before local installation;
the human then reloads Gameplan, obtains genuine loaded-code evidence and
starts 15m. Publication, installation and verified runtime recovery remain
separate facts. Scout remains shadow-only.
