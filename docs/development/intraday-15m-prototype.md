# Intraday 15m implementation and historical measurements

The shared implementation follows the [current design](../15m-trading-system/INTRADAY_15M_SYSTEM_DESIGN.md):
fixed not-down targets, full per-symbol retraining, independent technical sizing,
local execution and complete own-trader logs. Prior structural and measured
research records below retain their exact historical meaning.

Every night is already authorized by Jeremy's October 13 standing instruction:
Atlas produces its Gameplan, Scout produces its Gameplan, Scout synthesizes the
two plans, and Scout sends the result to Atlas. Delivery completes the handoff.
No further human permission, peer acceptance, third account snapshot or
cross-trader verification/reconciliation is an operating prerequisite. Each of
Gameplan, Atlas 15m and Scout 15m executes independently and keeps its own logs.
The first trader start remains Jeremy's manual action. Source publication and
private-data procedures continue separately from this operating authority.

## Components and offline verification

The old structural modules use the standard library. The new ensemble modules
use the existing NumPy/pandas/scikit-learn/XGBoost/joblib/psutil environment.
The manual adapter additionally reuses existing stock-trader contracts,
local journals and prepared Schwab transport. Retained shared-authority/native
ledger modules describe the earlier implementation, not cross-trader prerequisites. The current Gameplan's
combined 22-symbol nightly stream and four horizons retain their operating
bindings. `Check-15m-Readiness.cmd` is read-only; `Start-15m-Trader.cmd` is an
explicit human manual launcher capable of real orders after runtime gates.
No schedule, live session or production-worker restart was performed here.

| Component | Implemented behavior |
|---|---|
| `prediction.py` | Opening-time completed candles, actual availability, immutable quarter-hour issues, exact not-down targets and append-only outcome evidence. |
| `sizing.py` | Retained legacy hourly-reference fixture multiplication and configurable rounding; not the current live size scale. |
| `technical_sizing.py` / `starter_sizing.py` | Modern frozen-snapshot causal indicators, approved independent 1.5%-equity/side-price scale, nine-term per-symbol BUY/SELL score, half-up desired shares and complete immutable explanations. |
| `capacity.py` / `intentions.py` | Actual whole-share capacity clipping and append-only desired/adjusted/order projections, including null sizing and zero capacity; forecasts are never rewritten. |
| `scout_local_execution.py` | Independent local broker account capacity, immutable intended/adjusted quantities, prepared POST and own exact-ID recovery with no Gameplan configuration or peer service. |
| `account_execution.py` | Retained Atlas shared-book implementation; its old inter-trader operating requirements are superseded. |
| `live_session.py` / `Start-15m-Trader.cmd` | Explicit local manual session, latest-completed/no-backlog forecasting, durable local execution/recovery and independent scoring. |
| `runtime_compatibility.py` | Retained engineering diagnostic for earlier shared-book implementation; not an independent trader prerequisite. |
| `readiness.py` / `Check-15m-Readiness.cmd` | Read-only source/binding/runtime/saved-evidence checks, separating source readiness from fresh broker preflight; no provider/broker calls or trader start. |
| `book.py` | Host-local offline account book with atomic reservations, exact identities, allocation ownership, fill attribution, account/exposure headrooms and durable history. |
| `execution.py` | Explicit mock venue seam, contemporaneous quotes, ask-BUY/bid-SELL proposal, unknown-submission recovery, status retries and cancellation reconciliation. |
| `cancellation.py` | Explicit 15m stream/reason policy using existing prepared Schwab DELETE and exact status evidence; acknowledgment never releases commitment by itself. |
| `execution_policy.py` | Pure offline decisions for the strict Pacific submission cutoff and reconciliation before a new forecast replaces an unfilled intention; no broker or book mutation. |
| `prototype.py` | Original explicit fixture runner, local binding checks, frozen sizing explanation and mock instruction mechanics. It is not the trained ensemble runner. |
| `data.py` | Exact targets, causal features and matured labels, single-source local history, anchored acquisition, persistent interval recovery and evidence-backed no-trade constituents. |
| `models.py` | Fresh independent fits, real probability interfaces, model/source/data/configuration versions, immutable local fitted artifacts and per-model failure/timing records. |
| `store.py` | Immutable cycle snapshots and forecasts, append-only candle/outcome revisions, intentions including zero capacity, and operational cycle events in host-local SQLite. |
| `runtime.py` | Full enabled ensemble retraining every 15m research cycle, immediate per-symbol persistence, durable partial-cycle recovery, automatic available-outcome scoring and workload measurements. |
| `evaluation.py` | Purged chronological validation and point-in-time walk-forward comparisons on common timestamps with explicit retraining schedules/windows. |
| `telemetry.py` | Minimal forecast/score monitoring packets, strict peer identity and schema validation, immutable receipts, producer-based freshness and serialized concurrent admission. |
| `app/ui/intraday*.py` | Read-only 15-Minute Duckets tab with model probability bars, matrix, metrics, calibration, disagreement, diagnostics, timing and freshness. |

Run the focused offline checks using the project's Python environment:

```text
python -B -m pytest tests/test_intraday_prediction.py tests/test_intraday_book.py tests/test_intraday_execution.py tests/test_intraday_execution_policy.py tests/test_intraday_data.py tests/test_intraday_models.py tests/test_intraday_store.py tests/test_intraday_runtime.py tests/test_intraday_evaluation.py tests/test_intraday_telemetry.py tests/test_intraday_ui.py -q -p no:cacheprovider
```

The manual/account extension has separate focused checks:

```text
python -B -m pytest tests/test_intraday_technical_sizing.py tests/test_intraday_starter_sizing.py tests/test_intraday_capacity.py tests/test_intraday_intentions.py tests/test_intraday_account_authority.py tests/test_intraday_native_ledger.py tests/test_intraday_account_execution.py tests/test_intraday_cancellation.py tests/test_intraday_live_session.py tests/test_intraday_runtime_compatibility.py tests/test_intraday_readiness.py -q -p no:cacheprovider
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
probability and model version. That old injected fixture does not train; the new
`runtime.ShadowRunner` fits each enabled model from scratch. It fixes the cycle
reference explicitly and permits late issues without shifting their targets.

Each symbol/reference slot has one immutable issued prediction. A partial
Atlas cycle retries missing symbols and reuses completed issues. A new
quarter-hour produces new issues. Outcome records identify genuine target
candles and distinguish completed, pending and missing observations. Equality
is not-down. Later data corrections append evidence without rewriting an issue
or earlier observation. The ensemble uses the settled 0.50 classification
threshold (equality is not-down), never an execution-confidence gate. Provisional
observations are excluded from final metrics.

The legacy fixture requires a caller-supplied eligible session. The new data
adapter uses the installed XNAS exchange calendar, holiday exclusions, standard
04:00–20:00 Eastern extended sessions and regular-session-only short days. This
ingestion envelope does not define order eligibility. The later October 12
decision sets a strict 16:45 Pacific submission cutoff; the special 04:00
next-open and final intraday prediction contracts remain unresolved. Final fetched
information can be saved immutably through `save_final_information`, with no
fabricated target or overnight inference. This preserves the future immediate
04:00 prediction requirement without substituting an intraday gap model.

The legacy fixture indicator seam uses 23 genuine contiguous completed candles for
trailing measurements: one-step momentum, 14-change simple RSI, 20-candle
typical-price volume-weighted average and simple moving average, 14-candle
simple ATR relative to price, and relative volume against a supplied causal
baseline. It records rates and second differences per 15m step. These are
explicit prototype indicator definitions, not a selected trained model or
score recipe. Missing early-session history remains a visible data gap.
Holdings, recent executed quantities and volume baseline carry availability
times; future inputs are rejected. Sizing also receives the preserved forecast
probability and must use its original candle inputs.

The human reaffirmed that sizing is a per-symbol technical-indicator calculus,
including EMAs, SMAs, RSI, Bollinger Bands and oscillators. Each symbol uses its
own causal inputs and sizing result, with versioned per-symbol parameters where
needed. The existing fixture input seam implements the subset listed above;
it is not yet the complete indicator collection or a production score recipe.
The approved live starter computes each own-symbol score independently; it
does not use a shared constant fixture score or Loop C options-sizing formula.
The legacy fixture candle type lacks the new data adapter's synthetic/no-trade
provenance, so it must not be used to hide filled intervals as observed candles.
The trained ensemble's feature vector is also separate from the sizing recipe.

The additional `technical_sizing.py` engine works directly with modern candle
records and the forecast's original snapshot/hash. It preserves source,
synthetic/no-trade and availability evidence and rejects changed original inputs.
Its configurable families include EMA, SMA, RSI, Bollinger Bands, stochastic,
momentum, VWAP, ATR and relative volume, with optional first/second differences.
The configurable interface retains explicit indicator periods, session/history
window policy and per-symbol BUY/SELL normalization, weights, bias and score
bounds. The approved `starter_sizing.py` configuration supplies these for manual
Atlas execution. Reused trailing math comes from the repository's pure
`SharedCalculations` library; no other strategy's options formula is adopted.

Each contribution and the input/policy hashes are returned with the result.
Missing configuration, required inputs or unresolved rounding produce a null
desired quantity and an explanatory status. A genuinely calculated zero stays
zero. Account facts and the reference size retain their availability times.
Capacity-adjusted quantity remains null in the pure sizing result. The manual
adapter separately persists that original result in `IntentionStore`, captures
actual capacity and appends concrete adjusted quantities and dispositions.
Research `ShadowRunner` remains forecast/scoring work rather than an implicit
live trading entrypoint.

Run `tests/test_intraday_technical_sizing.py` and
`tests/test_intraday_readiness.py` alongside the focused checks above. Generic
engine recipes remain fixtures; `test_intraday_starter_sizing.py` verifies the
separately approved starter, without claiming its predictive quality.

## Policy seams retained explicitly

The legacy `HourlyReference` APIs/tests remain available for historical callers.
Their hourly multiplier, selectable tie rule and rounded-equality seam no longer
define manual 15m sizing. The approved starter uses
`0.015 * current account equity / fresh BUY ask or SELL bid`, multiplied by its
own-symbol score in `[0,1)`, nearest-rounded half-up with equality allowed.
It requires no due Gameplan instruction, hourly reference, horizon budget,
cash set-aside or reserve buffer. The initial periods and exact nine-term
normalization are in the
[consolidated sizing contract](../15m-trading-system/INTRADAY_15M_SYSTEM_DESIGN.md#3-approved-independent-technical-sizing).
All recipe/inputs/contributions and reference/forecast hashes are saved locally.
There is no minimum-trade veto, hourly aggregate cap or forced position close.

Legacy fixture SELLs carry explicit allocation/ceiling policies. The later
manual contract clips instead to actual eligible uncommitted same-symbol shares
across native lots and observed unallocated holdings. Native slice order is
stable accounting, not a horizon eligibility or pacing gate. It can span lots
while preserving each actual ownership reduction. Gameplan's longer signals
and cancellation selectors remain fenced away from 15m inventory/orders.

The reviewed legacy bearish-sale rules remain in the existing source: fallback
only when own inventory is genuinely absent, at most one donor per forecast,
50% shared daily and allocation ceilings, 18 opportunities/24 weight units,
simultaneous-instruction exclusions and pending-order restrictions. Those rules
remain Gameplan rules. They are not copied into the approved independent 15m
path and are not removed from Gameplan.

Legacy offline admission fixtures keep their historical identity/transaction
semantics. Their Scout rejection and shared-service design are not current
operating restrictions. Independent local execution requires no distributed
protocol or shared-drive database.

## Local accounting and own-order recovery

The current [Scout submission contract](intraday-scout-submission-contract.md)
uses its existing broker directly at manual startup. Full desired quantity is
saved before broker-observed cash or held-minus-pending shares clips the trade.
Local journals record one send, exact accepted IDs, cumulative fills and
cancellation state; a timeout cannot authorize another POST. A newer Scout
forecast waits only for its own prior order's terminal evidence before replacement.

Each trader keeps its own logs and controls. There is no cross-trader account
ledger, runtime proof, common lock, enrollment, acceptance or reconciliation
requirement. Missing cost basis and P&L remain unknown. Existing historical shared
book tests and receipts are preserved as engineering history, not current gates.

The nightly flow is independently preauthorized: Atlas plan + Scout plan,
Scout synthesis, then delivery to Atlas completes the handoff. Actual order
quantities are sized by the executing trader using its current broker capacity.

## Research configuration, evidence and remaining work

The initial independent scale, own-symbol TA recipe and half-up rounding are
approved and implemented. Future model/recipe/lookback comparisons,
any future same-forecast repricing policy and
special final-information/04:00 next-session targets remain separate work.
The manual launcher source is now provided alongside the research command;
only an explicit human live invocation can call order APIs. This change does
not itself launch it, change live schedules or restart Gameplan.

October 12 authorizes development, training, historical evaluation, read-only
existing provider access, shadow forecasting/scoring and development UI. Full
retraining of every enabled model on every symbol every 15 minutes is the initial
benchmark. No daily/incremental/warm-start/older-model substitution is permitted.
Use all compatible available history initially and measure the complete workload;
shorter windows and other cadences are separately named research comparisons.
The primary runner's `retraining_minutes` must be 15. The first research worker
setting is one symbol at a time with one internal model thread; this is explicit
and configurable, not a selected production resource policy. Each symbol is
persisted immediately on completion. No model timeout or execution fallback is
selected. Research overruns retain their explicitly selected replay/watch
policy. The manual worker selects latest-completed/no-backlog admission, logging
missed boundaries while keeping issued targets fixed. Its ordinary-session
bounds remain provisional for special opening/final-information contracts;
no overnight inference is fabricated.

Completed-cycle recovery preserves the original measured workload. Partial
recovery retains separate attempt events, reused/retrained symbol counts and
cumulative work across attempts; it does not relabel a near-zero reuse pass as
a fresh full training cycle. Store connections close deterministically and
recovered snapshots, forecasts and outcomes are checked against their saved
hashes before reuse.

The new tab reads only persisted evidence and does not start a provider, model,
trader or worker. Live, shadow and historical replay are distinct saved modes.
Local UI refresh does not advance remote producer timestamps. Capacity-adjusted quantities
do not alter forecasts. Missing quantities mean sizing is unresolved; no mock
size, fill, P&L or remote activity is displayed as real.

The first ten classifiers are Logistic Regression, Gaussian Naive Bayes, KNN,
LDA, Random Forest, Extra Trees, Histogram Gradient Boosting, Gradient Boosting,
AdaBoost and XGBoost. Each has a fresh preprocessing/training pipeline for the
exact target. LightGBM, QDA, MLP and chronologically calibrated Ridge/Passive
Aggressive adapters are selectable alternatives. CatBoost is absent from the
inspected environment. Prior neural architectures remain individually identified
as unsupported exact-target adapters; their TensorFlow/Keras (and TCN) dependencies
are not installed. Installed Torch alone does not establish compatibility.
Prior GaussianNB output rewriting and uncalibrated margin sigmoids are not reused.

Confirmed synthetic no-trade labels and outcomes are included in training and
headline scores under the explicit October 12 human choice. Every metric also
reports observed/synthetic denominators and breakdowns. This never authorizes
filling unknown gaps: repeated successful empty evidence and provider coverage,
an eligible session and an adjacent verified same-session prior close are required. Constituent gaps are
resolved individually; mixed observed/synthetic 15m bars retain their full
provenance and a synthetic marker.

The manual path excludes legacy stored synthetic rows without explicit prior
price/time continuity proof from new live inputs and outcome selection, while
retaining their original data, forecasts and prior score revisions for audit.
It never bridges a closed session or an unacquired price interval with a stale
close. This qualification applies to earlier reported synthetic outcomes too;
those prior measurements are not a claim of newly validated provenance.

The provider convention is documented in Databento's
[OHLCV schema](https://databento.com/docs/schemas-and-data-formats/ohlcv): absent
trades produce no bar, and timestamps name interval starts. That convention is
combined with exact covered responses and repeated evidence; it cannot establish
coverage or distinguish publication lag on its own.

The inspected EQUS.MINI minute history begins May 11, 2026 and ends October 9,
2026. Historical receipt/publication times are absent, so event-close availability
is explicitly assumed for replay. Other sources are not merged. One-to-five-year
compatible history is not established. Missing minute constituents currently
remain unavailable unless independently confirmed no-trade evidence exists.
All raw data, model artifacts, local databases and detailed run evidence stay
in local DATASTORE; shared-source publication contains code/tests/documentation.

Standalone invocation (substitute this PC's existing bindings):

```text
python -B -m ml.intraday.runtime --datastore LOCAL_DATASTORE --profile LOCAL_PROFILE --watchlist LOCAL_WATCHLIST --mode historical_replay --reference EXACT_UTC_REFERENCE
```

The command records actual fitting/processing times and assumed historical
information clocks separately. Repeated identical invocations recover the same
immutable cycle. A new explicit experiment identity is needed for a distinct
comparison. It does not install a schedule, start a trading session or restart
the production application.

For an explicitly started research watch, add `--mode shadow --watch
--overrun-policy retain_due_cycles` and optionally `--cycles COUNT`. Acquisition
stays on the quarter-hour grid plus the candidate offset, with a durable cursor.
The explicit research overrun setting processes due cycles late without moving
their targets. It does not select eventual execution handling. The opening and
final-session exceptions preserve information and report unresolved policy.
Provider recovery has no retry-count ceiling and honors persisted waiting
evidence after interruption. No native task is registered by this command.

## Measured Atlas research runs, October 12, 2026

The following are prior forecasting/replay measurements, retained unchanged.
They are not live-execution measurements for the manual-account source
extension, verified offline without launching a session.

Six complete eleven-symbol runs fitted all ten enabled models independently:
660 successful fits, 66 immutable forecasts, zero failed models. The experiment
used all compatible history available at each causal cutoff, with no 90-day
truncation. The following are measured seconds on Atlas, not estimates:

| Reference (UTC) | Source acquisition | Compute and persistence | Total |
|---|---:|---:|---:|
| October 9, 19:45 | 60.140 | 42.673 | 102.813 |
| October 6, 14:00 | 0.026 | 40.816 | 40.842 |
| September 17, 19:15 | 0.029 | 38.388 | 38.417 |
| September 17, 19:30 | 0.027 | 44.401 | 44.428 |
| September 17, 19:45 | 0.027 | 48.290 | 48.317 |
| September 17, 19:15, final recovery revision | 0.027 | 39.786 | 39.814 |

The first acquisition read, validated and aggregated native local minute files.
Later runs loaded a private verified history cache. None of these timings
includes a live price-data request. The September 17 19:15/19:30/19:45 references
are consecutive quarter hours, with all 110 models freshly fitted at each cutoff.
The final 19:15 run is a separately named repeat after the persistence/recovery
fixes; prior forecasts and experiments retain their own identities. It used
37.41 process CPU seconds and ended at approximately 407 MB process
RSS; these are recorded samples, not peak resource measurements. No Gameplan
worker was detected, so coexistence with a running Gameplan remains unverified.
The final workload overlapped a separate local research comparison; it is an
observed timing under that load, not an isolated hardware benchmark.

The local history supplied 2761/2758/1757/264/2748/2731/2814/2775/2338/2731/493
complete fifteen-minute candles for AAPL/AMZN/COST/CROX/GOOG/IONQ/MU/NVDA/PATH/
SNDK/TWST respectively. These are coverage observations, not ownership defaults.
Sparse intervals without confirmed no-trade evidence were left missing. An
additional September 17 19:00 cycle encountered missing required references for
COST and CROX: it recorded their data failures and issued nine genuine forecasts
from 90 successful fits. It did not claim a complete eleven-symbol workload.
Across these seven experiments there are 75 saved forecasts, 40 observable
outcomes and 35 missing exact targets. The earlier replay-scoring command also
exposed a backdated-observation rejection; recovery now skips later recorded
outcomes when asked to score at an earlier historical cutoff. Regression checks
verify neither rewriting nor exposing future outcome evidence.
No synthetic outcome was manufactured for this sample. This small, selected
replay sample does not establish predictive performance or reliable calibration.
Historical information availability is assumed from closes, explicitly separate
from actual acquisition/fit/recording clocks. Prior outcomes and failed early
scoring revisions remain auditable; forecasts were never rewritten.

Existing-provider metadata reported EQUS.MINI minute coverage starting March 28,
2023 and ending October 10, 2026 at 00:00 UTC during a 3.887-second read-only
metadata check. That advertised envelope is not locally acquired compatible
history, no-trade proof for an unqueried interval, or a measured publication
offset. No additional historical price download or spending was performed.

After the human launched Gameplan, a separate pre-session run at 03:47:31–03:48:13
Pacific fully fitted all 110 models and published eleven forecasts in 41.722
seconds (0.033 cached-history acquisition and 41.689 compute/persistence), with
no failures or provider requests. It used 38.969 process CPU seconds and ended
at approximately 404 MB RSS. Current as-of evaluation then had 86 forecasts,
51 scored observed outcomes and 35 missing exact targets; prior records remain
unchanged. This adds timing evidence, not an accuracy or calibration claim.

The Gameplan worker reported `SLEEPING_UNTIL_OPEN` for 04:00 Pacific. Separate
process observations bracketed this run; a later 55-second passive observation
showed both processes still present with zero additional CPU seconds. That
passive observation began after fitting finished and does not establish
simultaneous CPU contention. The run exposed a telemetry defect: its stored
worker count was zero because the counter recognized only older entry points.
The counter now recognizes the actual `ml.gameplan_stock_trader` module and
retains the two legacy module identities, while rejecting module-like text in
unrelated script or `-c` arguments. The original measurement is retained with
this correction; worker presence does not imply an active trading session.

Gameplan readiness has operational priority. The separate 15-minute shadow
runner can start independently at a later eligible quarter hour once its
required data is verified; Gameplan startup is not its prerequisite. No further
training load was started while prioritizing the human's live session startup.
Those research samples did not measure active-session contention or live
provider availability. Later human-started observations are recorded below.

The installed environment used scikit-learn 1.9.0, NumPy 2.5.1, pandas 3.0.3,
XGBoost 3.3.0 and psutil 7.2.2. The import path also requires joblib,
threadpoolctl, exchange_calendars and filelock; optional provider mode reuses
the existing Databento client and dotenv binding. These dependencies are already
declared by the project's base dependencies and `ml`/`ml-tree` extras; an
environment installed from only the core project is insufficient for training.
Source tests separately check
fresh fits, probability interfaces, overlapping-label purge, model failures,
exact immutable targets, zero-capacity intentions, recovery, no-trade evidence,
per-symbol publication and absence of broker order dependencies. Historical
schedule/lookback comparison machinery is implemented, but broad comparative
experiments and multi-year workload measurements remain to be run.

A small actual walk-forward comparison used the same three AAPL references on
September 17 at 18:45, 19:00 and 19:15 UTC, selected because their exact targets
were observed. All-history training used 2138/2139/2140 matured rows. The 15m
schedule freshly fitted 30 model instances over three ensembles in 21.735 seconds;
the explicitly named 30m comparison fitted 20 instances over two ensembles in
16.168 seconds. Both produced three scored forecasts, two correct, and zero late
issues or model failures. These selected three points establish functionality,
not schedule superiority. Their execution overlapped the final eleven-symbol
benchmark and is subject to process warm-up and run-order effects. The evaluation
records nominal cutoffs, simulated issues after measured work/queue delay, fixed
targets and actual computation clocks separately. A failure never creates an
issued forecast; timely and late outcomes have separate metric breakdowns.

A separately named 30-calendar-day, 15m-retraining experiment on those same three
references admitted 501/501/502 matured training rows and fitted all 30 model
instances in 8.951 seconds. It also scored two of three targets correctly, with
zero late issues or model failures. The window is explicitly anchored to each
training cutoff, with earlier observations used only for causal feature warm-up.
This small comparison does not select a shorter production window or replace
the all-available-history, full-15m benchmark; timings were not collected under
identical background load.

## Human-started Atlas recovery observations, October 12, 2026

These are historical incidents in the earlier shared-book implementation. Their
then-required compatibility/lock procedures are not the current operating policy.

The initial manual cycle freshly fitted all 110 models in 187.963 seconds;
Gameplan subsequently exited on shared-lock contention. After the reviewed
lock and fill-timestamp repairs and human reloads, saved evidence showed
Gameplan waiting for that shared lock and clearing the wait normally.
These are bounded observations, not proof of sustained operation or accuracy.

The 11:02 Pacific manual start completed its first forecasting pass in 300.682
seconds, excluding initial history loading. Eight symbols produced full
ten-model ensembles (80 fresh fits). Three failed before training because
their exact 10:45-11:00 reference intervals required a verified adjacent prior
close. Their fixed targets remained 11:30. Valid predecessor observations
arrived later, but the old live loop did not revisit those failed symbols.
This measured duration includes acquisition waiting and partial workload;
it is not a successful 110-fit benchmark or training-only timing.

Before this repair was installed, the following two human-started session
cycles each completed all eleven full ensembles (110 fresh fits) in 535.085
and 287.764 seconds. Read-only saved evidence at 11:43 Pacific showed both
Gameplan and 15m running. The first eight forecasts then had seven observed
and one synthetic scored outcome. These samples show actual coexistence and
scoring progress under the prior loaded source, not verification of this
repair or predictive quality; they are not controlled performance comparisons.

The recovery repair retains pending references and retries bounded acquisition
passes with refreshed same-source saved predecessors. Confirmation learned
later requires a new interval response; information availability is not
backdated. Covered repeated leading omissions can also trigger bounded
acquisition of the missing same-session predecessor from that same source.
A newly due reference has priority over pending older passes.
Already persisted forecasts remain immutable and are not trained or issued
again. Recovered forecasts from older quarters are scoring-only once newer
quarter-hour work has begun, even if their targets are still in the future.
The fixed target and normal live eligibility checks also apply to recovered
forecasts. Interrupted recovery uses durable cycle evidence for the
same manual session; a new manual session does not inherit prior execution
authority.

For current shadow/live cycles, `total_cycle_seconds` and the 15m overrun flag
now measure elapsed wall time from original cycle start, including waits
between bounded passes. `cumulative_attempt_seconds` and the stage counters
retain actual work measurements separately. The measurement scope is recorded
as `wall_elapsed_with_cumulative_attempt_work_v1`; historical replay keeps its
existing work-time measure. Earlier cycle records retain their original scope.
The read-only UI and new telemetry packets retain the optional scope and work
counters. Older packets without a scope remain readable, but their totals
cannot be assumed comparable to explicitly measured wall-time totals.

An independent offline reproduction also confirmed that a pending BUY quote
received during account capture could be rejected against the earlier request
start. The Gameplan adapter now records validated information readiness after
identity verification while preserving original capture age and quote times.
The original future/stale/price checks remain. Raw per-quote timestamps were
not saved for the live failure, so its exact offending quote delta is unknown.
See the [live preparation record](intraday-live-readiness.md) for limitations
and the manual reload boundary. Final offline checks are bound to the exact
published source receipt; prior test counts do not verify this repair.

## Read-only cross-PC monitoring

`python -B -m ml.intraday.telemetry export --database LOCAL_DATABASE --pc Atlas
--source-version EXACT_SOURCE_VERSION --output REVIEWED_PACKET` creates a
minimal packet from real persisted results. It includes latest per-symbol/mode
forecasts, individual probabilities, outcomes and cycle measurements. It excludes
raw candles/features, fitted artifacts, account state, quantities, orders,
private error text and machine paths. Exporting a file is not transport delivery.
Review the exact output under the installed artifact/private-export rules before
using an authorized transport; no automatic network publisher is installed.

On the receiving PC, `python -B -m ml.intraday.telemetry import --packet
RECEIVED_PACKET --receipts-root LOCAL_RECEIPTS --expected-peer Atlas --local-pc
Scout` verifies schema, hash, identity and time, retains the first receipt, and
rejects changed immutable forecasts or conflicting outcome revisions. Reverse
the identities for Atlas receiving Scout. A host-local file lock serializes
concurrent admissions. The UI optionally reads the exact receipt named by
`DUCKETS_INTRADAY_PEER_RECEIPT`, with expected identities from its local profile.
No setting is silently installed or changed.

Peer freshness uses original producer and forecast times separately from local
receipt and UI refresh times. Historical-only packets stay historical-only.
Remote metrics describe the received snapshot rows, not a complete peer history.
Offline tests exercise both identities, tampering, duplication, stale/out-of-order
packets and two-process conflict admission. Scout's actual source adoption and
live cross-PC receipt/freshness check require evidence from Scout; Atlas-local
tests do not establish them. Coordination is tracked in
[atlas-scout issue 18](https://github.com/jeremysecondstate/atlas-scout/issues/18).

An Atlas-local real-store protocol rehearsal exported 11 latest replay rows and
admitted them into a separate test receipt directory. The received status was
`historical_only`; refreshing or receiving it did not turn replay evidence into
a fresh shadow forecast. This rehearsal did not run on Scout or deliver a packet
to Scout. The saved forecasts remained byte-for-byte unchanged after the
final scoring pass.

Subsequent prior exchange evidence records Atlas's authentic receipt of Scout's
packet at `2026-10-12T12:37:14.556354+00:00` and Atlas's packet publication at
artifact commit `61ec8dbc4171ba31a2884e5644eb94d62944d4e6`, as recorded in
[issue 18's exchange receipt](https://github.com/jeremysecondstate/atlas-scout/issues/18#issuecomment-6124501809).
Scout's independent receipt of the Atlas packet remains outstanding. Its 22
Atlas rows are historical replay source cohorts, not fresh shadow/live evidence.
No new receipt is claimed for this source extension; continuous live telemetry
delivery is not installed.

The reviewed source candidate includes the manual Atlas extension. Production
workers were not restarted or deployed, live schedules/bindings were not
changed, and no broker orders were submitted for this development. Source
publication, main integration, local installation, loaded runtime and human
live launch require their own exact receipts/evidence.
