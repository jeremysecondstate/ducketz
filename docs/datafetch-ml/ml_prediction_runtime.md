# Duckets Loop B runtime

Implementation snapshot: 2026-08-01

Loop B is one recurring ML supervisor. It consumes Loop A values, builds
point-in-time samples and targets, trains or reuses models, generates
predictions, reconciles matured outcomes, calculates evaluation metrics, and
refreshes current intelligence outputs. In the same cycle it runs the
versioned options-strategy stage, builds causal observed-BBO strategy outcomes,
fits or reuses route strategy models where evidence is sufficient, and
publishes exact-chain candidates and per-strategy audits.

The implementation is intentionally file-oriented and auditable. Run history is
organized by UTC timestamp. Rows use readable natural columns rather than
internal identity chains.

## Command

One cycle:

```powershell
python -m ml.prediction_runtime `
  --datastore C:\data\duckets `
  --symbols NVDA `
  --provider databento `
  --horizons 1h 4h 1d 1w `
  --once
```

This uses the default `loop-a-all-v1` profile. Its four public feature
inventories are registered as version `1.2.0`. Public `1w` expands internally
to aggregate `1w` plus `1w-d1` through `1w-d5`; operators never have to list
those six values. The runtime consumes every
applicable column in those active sets; the separately registered candidate
readiness reports are audit metadata, not an admission gate for this profile.

`--feature-profile` (alias `--feature-set-profile`) accepts exactly:

| Profile | Selected feature sets |
| --- | --- |
| `loop-a-all-v1` | `loop-a-all-v1-1h`, `loop-a-all-v1-4h`, `loop-a-all-v1-1d`, and `loop-a-all-v1-1w` |
| `production-v1` | legacy 19-column `technical-all` for `1h`/`1d`/`1w` and horizon-scoped `technical-all-4h` for `4h` |
| `technical-all-v2` | horizon-specific `technical-all-v2-1h`, `technical-all-v2-4h`, `technical-all-v2-1d`, and `technical-all-v2-1w` |

There is no arbitrary feature-set override.

Omitting `--horizons` selects the canonical public default `1h 4h 1d 1w`.
Selecting `1w` materializes up to six internal weekly model routes. Each
symbol's LIVE output independently contains aggregate `1w` and the Day 1
prefix remaining in its current target exchange week. A missing or late weekly
decision omits only that symbol's weekly LIVE rows unless the operator opts
into `--require-all-routes`. The public behavior of `1h`, `4h`, and `1d` is
unchanged.

Recurring supervisor:

```powershell
python -m ml.prediction_runtime `
  --datastore C:\data\duckets `
  --watchlist datafetching\watchlist.txt `
  --provider databento `
  --horizons 1h 4h 1d 1w `
  --interval-minutes 60
```

Available model and target options:

```text
--model-family logistic|lightgbm|xgboost
--calibration none|platt|isotonic
--balanced-class-weight
--minimum-train-clusters N
--calibration-clusters N
--assessment-clusters N
--lockbox-clusters N
--round-trip-cost RATE
```

`--round-trip-cost` must satisfy `0 <= RATE < 1`. All cluster counts and
`--interval-minutes` must be positive.

The separately versioned `schwab-spreads-v1` strategy-analytics stage runs in
every Loop B cycle; there is no enable/disable profile. It reads immutable Loop
A normalized Schwab contract, option-surface, and stock-quote receipts,
excludes the real lockbox, and publishes schema-bound candidate and
per-strategy audit artifacts for every attempted current concrete route. Every
numerically constructible candidate remains visible; quote quality, liquidity,
model availability, and route rank are explicit columns. A route that cannot
yet fit its chronological strategy partitions still publishes exact-chain rows
ranked by a pricing-informed scenario probability. Those rows carry
`PRICING_SCENARIO_FALLBACK`, a raw profitable-outcome probability and separate
expected-return measurements; the calibrated probability remains null until
compatible pricing-enhanced evidence supports fitting and calibration. The
route model report separately records `MODEL_NOT_FIT`.
Unavailable chain history is recorded in the 40-strategy route audit rather
than converted into a recommendation. Loop B analytics do not submit orders.
The separate,
user-controlled Options Strategies screen can construct and submit a confirmed
Schwab order from a selected candidate. Authoritative readers resolve both
strategy Parquets from the immutable run selected by `ml/latest/run.json`.
See [Loop B options-strategy selection](options-strategy-selection.md).

The four partition values count target clusters, not calendar dates. A target
cluster is one distinct `target_window_start` value and contains all samples
with that value; `1h` and `4h` consequently have multiple clusters in one
calendar day. When a cluster-count option is omitted, its value comes from the
selected horizon's default. Supplying an option overrides that count for every
selected horizon.

`--symbols` overrides `--watchlist`. If neither is supplied, Loop B preserves
the legacy behavior of discovering symbol directories under `DATASTORE/stocks`.
The selected symbols are resolved once before the recurring supervisor starts;
restart Loop B after changing its watchlist. Use either `--datastore PATH` or
`--datastore-target pc|local`.

## Supervisor behavior

Before entering the loop, the command creates:

```text
DATASTORE/.duckets-ml-prediction-runtime.lock
```

The lock contains the process ID and start time. If it already exists, a second
supervisor fails before reading or writing run data.

At each recurring phase, Loop B acquires
`DATASTORE/.duckets-loop-a-cycle.lock`, then requires
`DATASTORE/.duckets-loop-a-cycle.json` to describe a `COMPLETE` Loop A cycle.
Loop A holds the same crash-released operating-system lock while it fetches and
calculates, and Loop B holds it through input reads, modeling, and atomic
publication. This prevents mixed-cycle ingestion without a bootstrap, lease,
decision handoff, acknowledgement, or retry state machine. Phase zero is
allowed; the default `+05` phase remains useful operational spacing.

Loop B may read the same `COMPLETE` Loop A cycle more than once. Non-weekly
routes retain their existing rolling behavior. Once a weekly snapshot is
issued, later runs carry forward the exact verified origin rows: probabilities,
model versions, `prediction_created_at`, and target windows do not change. If
the latest Loop A state is `WRITING`, `FAILED`, absent, or unreadable, Loop B
does not publish and tries again on its next normal schedule.

`Ctrl+C` is caught at the supervisor boundary, prints a clean stop message, and
removes the process lock.

With `--once`, exactly one cycle is attempted against a `COMPLETE` Loop A state.
The command returns `0` on a completed cycle and `1` if the cycle raises an
exception.

## Input contract

For each symbol and selected horizon, Loop B reads:

- adjusted normalized prices from the selected provider and source timeframe;
- current `market-regime` technical values;
- current `breakout-pressure` technical values;
- bar-shape, weekly-context, and technical-lifecycle signals where applicable;
- FMP fundamental-direction and point-in-time fundamentals;
- fundamental-technical lifecycle signals;
- Schwab quote-liquidity and option-quality/realized-volatility evidence;
- receipt-verified compact Pricing surfaces when the active-v2 profile is
  selected;
- FMP energy, FRED macro, SEC event, and Databento CME context.

The default feature and decision-source timeframes are:

| Horizon | Feature and decision-source timeframe |
| --- | --- |
| `1h` | `1h` |
| `4h` | `1h` |
| `1d` | `1d` |
| `1w`, `1w-d1` ... `1w-d5` | `1d` |

The active default sets contain:

| Horizon | Feature set | Ordered model columns | Family counts |
| --- | --- | ---: | --- |
| `1h` | `loop-a-all-v1-1h` | 69 | market regime (`mr`) 13; breakout pressure (`bp`) 13; bar shape 2; technical lifecycle 5; quote 1; options 26; energy 1; CME 8 |
| `4h` | `loop-a-all-v1-4h` | 69 | market regime (`mr`) 13; breakout pressure (`bp`) 13; bar shape 2; technical lifecycle 5; quote 1; options 26; energy 1; CME 8 |
| `1d` | `loop-a-all-v1-1d` | 139 | `mr` 13; `bp` 13; bar shape 3; weekly context 3; technical lifecycle 5; fundamental direction 25; point-in-time fundamentals 13; fundamental-technical lifecycle 17; quote 1; options 32; energy 1; macro 4; SEC 3; CME 6 |
| `1w`, `1w-d1` ... `1w-d5` | `loop-a-all-v1-1w` | 132 | `mr` 14; `bp` 12; bar shape 3; weekly context 3; technical lifecycle 5; fundamental direction 25; point-in-time fundamentals 13; fundamental-technical lifecycle 17; options 29; macro 4; SEC 3; CME 4 |

The `1h` and `4h` routes deliberately use completed canonical `1h` bars for
decisions and features while using canonical adjusted native Databento `1m`
bars only for their predetermined target prices. Native hourly rows are
preferred at duplicate timestamps; an all-60-constituent `1m` aggregate fills
only a native publication lag. The `4h` route reuses the same `1h` source cache
as `1h`. Loop A does not fetch or persist synthetic `4h` bars, and target minute
bars never enter model features.

The weekly family creates six historical candidates after each eligible daily
decision. Components Day 1 through Day 5 predict each future eligible session's
open-to-close direction, while aggregate `1w` predicts Day 1 open through the
final eligible close of Day 1's exchange week. This daily rolling cadence
preserves training density. LIVE issuance uses the latest completed daily
decision and publishes only the same-week Day 1 prefix.

The feature registry selects explicit columns. It does not discover arbitrary
numeric columns. The default `loop-a-all-v1` profile selects one explicit,
ordered feature set per horizon and persists that order with each model. The
legacy technical-only profiles remain selectable for compatibility. Family
contracts are in
[`audited-feature-contracts.md`](audited-feature-contracts.md).

The production command selects `loop-a-all-bsgp-active-v2`, which adds the same
11 ordered `opx__` columns to every horizon without changing or substituting the
rest of the feature contract. Its v2 surface reader is strict. An exact
receipt-, manifest-, checksum-, and physical-schema-verified legacy v1 surface
with policy `black-scholes-rbf-residual-v1` is the only compatibility case:
Loop B derives canonical availability as
`max(row.available_at, publication.published_at)` in memory and records the
source publication/surface versions and normalization policy. It never rewrites
the legacy authority.

`loop-a-all-v1-4h` is a horizon-scoped clone of
`loop-a-all-v1-1h`: exactly the same ordered 69 model values and family
composition. The `4h` technical and bar values keep their existing exact
decision/as-of behavior; freshness is two days for technical lifecycle, five
minutes for quote, two hours for options, 30 minutes for energy, and 15 minutes
for CME. The implementation does not widen the existing feature definitions,
so the established `1h`, `1d`, and `1w` semantic fingerprints remain unchanged.

Feature inputs are joined by readable `symbol` and timestamp values. Adjusted
bar prices are aligned to the same exchange sessions. No observation or sample
identifier is required for an input join.

The physical source contract is fail-closed. For every requested symbol, each
family applicable to the selected profile must have its required non-empty
Parquet input and required columns. Families routed through the generic adapters
must also contain at least one populated numeric family value. A missing or
structurally empty input makes the affected materialization route non-ready; the
supervisor then rejects the entire cycle. The `opx__` family is the deliberate
exception for valid unavailability: no causal publication, no coverage, an
empty verified surface, staleness, or a failed surface-quality row produces
null `opx__` values rather than destroying an otherwise viable route.
Malformed clocks, mixed or unknown versions, checksum or physical-schema
failure, and automation-enabled Pricing rows remain fatal to the complete
publication.

Presence is not the same as per-row completeness. A source can be present while
a particular decision has no publication at or before its decision time, its
latest publication is stale, or a specialized loader rejects that row's quality.
In that case the family columns for that decision remain null. The feature
columns still exist in the sample schema, and the model's training-fitted
imputation and missing indicators handle those nulls. For `opx__`, the manifest
also records per-route join-status counts, missing-row counts, source status,
publication/surface versions, authority path/time, and legacy normalization
policy.

Operational point-in-time wiring has three levels:

- market-regime and breakout-pressure use strict exact technical alignment;
- bar-shape, weekly-context, and SEC use their specialized family loaders;
- technical lifecycle, fundamental direction, point-in-time fundamentals,
  fundamental-technical lifecycle, quotes, and options use the generic
  symbol-scoped backward-as-of adapter; energy, macro, and CME use the generic
  shared-context backward-as-of adapter. Pricing uses the symbol-scoped adapter
  with `valid_until` equal to the earlier of canonical first availability plus
  horizon freshness and original `target_snapshot_for` plus the same freshness.
  Republishing a cumulative surface therefore cannot renew an old market
  target.

The generic adapters require `available_at <= decision_timestamp`, apply the
configured freshness rule where one exists, and deterministically keep the last
same-availability row after their configured ordering. Stricter family helper
APIs elsewhere in the repository do not all gate this operational path.

Macro is a deliberate current-receipt limitation. Loop B reads the normalized
current observation histories for FEDFUNDS, CPIAUCSL, UNRATE, and GDP, derives
one context row, and sets its availability to the maximum selected
`fetched_at`. It does not reconstruct historical FRED releases or vintages.
Decisions before that current receipt therefore have null macro values.

Loop B also requires each current `market-regime` and `breakout-pressure`
Parquet to record the same `price_adjustment_status` and `split_event_count` as
the current adjusted bars. A missing or different value rejects that route
instead of combining technical values calculated on a stale split basis.

### Exact Schwab inputs for strategy analytics

The strategy stage does not reconstruct legs from the aggregate `opt__*`
feature row. For each symbol it loads the immutable normalized Schwab chain
history under `options/chains/schwab/normalized`, the matching option-quality
surface history under `options/features/option-quality/schwab`, and stock BBO
receipts under `quotes/features/quote-liquidity/schwab`. It enforces option-chain
schema `1.1.0`, option calculation/schema/quality contracts `1.2.0`,
`option-surface-v2`, and `schwab-option-surface-quality-v1`, and stock-quote
schema/quality contracts `quote-liquidity-v1` and
`schwab-quote-quality-v1`.

An entry surface must have `snapshot_for` at or after `bar_end_timestamp` and
no later than the causal cutoff. Its `available_at` must be at or after
`information_available_at` and no later than the earlier of the Loop B
input cycle's `finished_at` or one nanosecond before target entry. Historical candidate
construction selects the earliest eligible entry surface; the current pass
selects the latest. Exact contracts are joined to the selected surface on
`symbol, snapshot_for, available_at`. Historical labels use the earliest
eligible future receipt after the target window, remain bounded by a
route-specific exit tolerance, and cannot cross the first real-lockbox start.
Exact contract-symbol continuity is mandatory. These receipt rules and the
complete feature lineage are audited in
[Loop B point-in-time feature audit](loop-b-point-in-time-feature-audit.md).

## Stage order

### 1. Materialize samples and targets

`ml.rolling_materialization` loads the route inputs and
`ml.rolling_samples` constructs exchange-calendar windows.

Each sample is unique by:

```text
symbol, horizon, decision_timestamp
```

Its `id` is the readable concatenation of those three values.

Target columns include:

```text
target_definition_version
target_specification
target_window_start
target_window_end
label_available_at
target_open
target_close
forward_raw_return
forward_cost_adjusted_return
target_cost_adjusted_positive
assumed_round_trip_cost
label_status
label_exclusion_reason
```

The exact revised intraday target definitions are:

| Horizon | Target-definition version | Eligible regular-session minutes |
| --- | --- | ---: |
| `1h` | `next-60-eligible-regular-minutes-open-close-v2` | 60 |
| `4h` | `next-240-eligible-regular-minutes-open-close-v2` | 240 |

Both use calendar policy
`session-open-break-resume-plus-full-local-clock-anchor-v1` and target-price
source `canonical-adjusted-native-1m-interval-open-v1`. The target procedure is:

1. a completed eligible canonical `1h` source bar supplies the decision features
   and `previous_period_direction`; information becomes available at the
   source-bar end plus the five-minute processing delay;
2. before any price lookup, each continuous regular-session segment contributes
   its exact start (official open or post-break resume) and every exchange-local
   clock-hour start whose full clock hour is contained in that segment;
3. the first candidate strictly after `information_available_at` becomes
   `target_window_start` and `actionable_until`; equality is too late;
4. from that start, the exchange calendar accumulates exactly 60 or 240
   predetermined eligible native one-minute intervals, pausing for breaks,
   overnight closures, weekends, holidays, and early closes;
5. `target_window_end` is the end of the final selected minute and
   `label_available_at` is that end plus five minutes;
6. `target_open` is the first selected native minute's open and `target_close`
   is the final selected native minute's close, so intervening closed-period
   price gaps remain in the return;
7. every predetermined first, middle, and final native `1m` record must exist;
   any missing constituent leaves the fixed window incomplete, with no
   substitution or shift; and
8. the configured round-trip cost is subtracted exactly once from the simple
   open-to-close return, and the positive class requires a strictly positive
   cost-adjusted return.

The weekly target procedure is independent of the intraday minute-count
procedure:

1. aggregate `1w` uses target version
   `dynamic-remaining-week-aggregate-open-close-v2`, while the components use
   `dynamic-remaining-week-d1-open-close-v2` through
   `dynamic-remaining-week-d5-open-close-v2`;
2. after each eligible completed daily decision, the exchange calendar selects
   the next five eligible sessions;
3. `1w-d1` through `1w-d5` use each selected session's official open and close;
4. aggregate `1w` starts at Day 1's official open and ends at the final
   eligible close in Day 1's exchange week;
5. each route subtracts the configured round-trip cost exactly once;
6. each component label becomes available at its own close plus processing
   delay, while aggregate `1w` waits for its dynamic final close plus processing
   delay; and
7. weekends, holidays, early closes, and DST come from the exchange calendar;
   the LIVE prefix stops at the exchange-week boundary.

The six historical routes use separate ordinary single-target models. They do
not recursively consume one another's predictions.

For an ordinary XNAS session, information from the prior completed session can
therefore produce a pre-open target beginning at the official 09:30 Eastern
open. The `1h` target ends at 10:30 and the `4h` target ends at 13:30 Eastern;
09:30–10:00 is not mislabeled as a one-hour target. The start is 14:30 UTC in
EST and 13:30 UTC in EDT (06:30 Pacific in either ordinary US season).
Ordinary intraday decisions retain full local-clock anchors: for example,
information available at 11:05 Eastern selects 12:00. A late target may cross
an early close, overnight closure, weekend, holiday, DST transition, or
exchange break; closed time pauses eligible-minute accumulation while any
endpoint price gap remains part of the return.

Targets remain null and `label_status` remains `INCOMPLETE_LABEL` until the
target window has ended, the processing delay has elapsed, and the required
prices are available. This prevents future information from entering training
rows.

The projected sample frame is held in memory during model work and
`samples.parquet` is written only after every required horizon's model and
prediction work succeeds. Its feature portion is the ordered union of the
active feature columns for all selected horizons. This gives one stable Arrow
schema for the run: for example, a `1h` or `4h` row has nulls in columns that
exist only in the `1d` or `1w` set. The union is a storage contract, not a
model contract. Each horizon model projects only its own exact ordered 69-,
69-, 139-, or 132-column subset in canonical order. All six weekly routes reuse
the unchanged 132-column `loop-a-all-v1-1w` membership; no numeric-column
discovery or feature expansion occurs.

### 2. Partition completed rows

Loop B pools all configured symbols within a horizon and fits or reuses one
model for that horizon. It does not fit one model per symbol. The same
horizon-wide model produces assessment predictions and the latest eligible
live prediction for each symbol.

Only rows with `label_status = COMPLETE` enter model partitions. Dates are
not the partition unit. Loop B orders the distinct `target_window_start`
clusters chronologically:

1. the earliest clusters form training;
2. the next clusters form calibration;
3. the next clusters form assessment;
4. the latest remaining clusters form a closed, untouched model-time lockbox.

The horizon-specific defaults are:

| Horizon | Minimum training clusters | Calibration clusters | Assessment clusters | Closed lockbox clusters |
| --- | ---: | ---: | ---: | ---: |
| `1h` | 252 | 63 | 63 | 126 |
| `4h` | 252 | 63 | 63 | 126 |
| `1d` | 252 | 63 | 63 | 126 |
| `1w` | 252 | 63 | 63 | 126 |
| `1w-d1` through `1w-d5` | 252 | 63 | 63 | 126 |

Partition purging is selected from the actual materialized target geometry,
not from a hard-coded horizon name. Whenever one target window reaches beyond
the next target start, rows are purged at all three transitions: training to
calibration, calibration to assessment, and assessment to lockbox. Loop B
selects sufficient earlier clusters so the configured minimum training,
calibration, and assessment counts survive purging; if it cannot, it fails
readiness clearly instead of automatically reducing a requirement. Aggregate
`1w` therefore purges overlap from its rolling remaining-week windows. Windows
that end exactly at the next start are
conservatively treated as sharing a target-price endpoint and are purged,
because their labels are not available before that boundary. The configured
lockbox cluster count must also remain satisfied. Assessment rows are reserved
for offline evaluation and backtest predictions; they are not used to fit the
estimator or calibrator.

Target starts carried by verified prior `LIVE` predictions are excluded from
offline partitioning, keeping genuine post-training outcomes available only
for live-evidence reconciliation instead of reclassifying them as lockbox
targets. Lockbox targets are never coerced, read, returned in persisted
`samples.parquet`, predicted, or scored. Model JSON records only
`CLOSED_UNTOUCHED_UNSCORED`, the row and target-cluster counts, and the
first/last `target_window_start` bounds. It stores no lockbox targets,
predictions, or performance metrics.

### 3. Train or reuse a model

The default is a logistic model with Platt calibration. LightGBM and XGBoost are
available when their dependencies are installed.

The consolidated route-to-default-model, predicted-outcome, and Python-file
mapping is in the
[models and probabilities matrix](rolling_forecasts.md#models-and-probabilities).

Before fitting or predicting, the model projects the horizon's exact ordered
registry columns and applies each feature's registered semantic transform.
Nonnegative and capped-log features use their declared `log1p` transforms;
`log1p-capped-training-v2` caps at the training-fitted 99.75th percentile
before applying `log1p`. The logistic numeric pipeline then performs median
imputation, training-fitted 0.25th/99.75th-percentile clipping, robust scaling,
and missing-indicator expansion. Its categorical branch one-hot encodes any
registered categorical columns. The tree-family pipelines use median
imputation plus missing indicators for numeric columns and one-hot encoding
for categorical columns. Statistics and caps are learned only from the
training partition.

A model is reused only when its manifest matches the requested:

- model family and readable model name;
- horizon;
- feature-set name/version, explicit feature order, canonical semantic feature
  contract, and its fingerprint;
- every feature's source, dtype, provider/timeframe/grain policy,
  schema/calculation versions, availability/delay, freshness, missing,
  transform, and readiness semantics;
- preprocessing policy version, fit partition, exact numeric clipping bounds,
  and semantic training-cap mapping;
- target column;
- for `1h` and `4h`, the complete readable horizon specification,
  target-definition version, target-price provider/timeframe/source version
  and constituent rule, calendar-policy version and definition, processing
  delay, one-time cost convention, numeric assumed round-trip cost, and
  strict-positive classification;
- for each internal weekly horizon, the readable horizon specification,
  target-definition version, and numeric assumed round-trip cost;
- recorded estimator parameters and any route-specific calibration parameters;
- calibration method;
- class weight;
- training, calibration, and assessment row counts plus the configured cluster
  counts and closed-lockbox count/bounds;
- training-through timestamp;
- horizon-scoped input file paths, sizes, and modification times;
- Python implementation and major/minor version;
- recorded package versions for NumPy, pandas, PyArrow, scikit-learn, joblib,
  exchange-calendars, LightGBM, and XGBoost.

Otherwise Loop B fits a new estimator and calibrator. Training requires both
target classes. If a calibration slice has one class, the effective calibration
method becomes `none`.

The aggregate `1w` logistic model has its own fitted-model policy: L1
regularization with `C=0.3`, `l1_ratio=1.0`, the `liblinear` solver, a
`5,000`-iteration ceiling, and tolerance `1e-5`. When Platt calibration is
requested and the calibration slice contains both classes, its Platt fit uses
`C=0.1`. At prediction time, the raw probability passed to that calibrator is
bounded to the raw-probability range seen in the chronological calibration
partition, so Platt scaling does not extrapolate beyond its fit support. If the
calibration slice has one class, the effective calibrator remains `none` and no
support clipping is applied. The estimator override is not applied to a
non-logistic aggregate model, and neither override is applied to the five
component routes (`1w-d1` through `1w-d5`). Aggregate estimator and conditional
calibration parameters are part of model compatibility and force a new
aggregate fit when changed.

A change to either intraday target contract invalidates model reuse only for
that horizon. Moving to the v2 definitions invalidates legacy `1h` and `4h`
reuse, while `1d` compatibility remains unchanged. The standard paths
are `DATASTORE/ml/models/1h/logistic-1h/...` and
`DATASTORE/ml/models/4h/logistic-4h/...`. An operator deployment must retrain
both affected horizons and restart Loop B before v2 forecasts can be observed;
this document does not claim that operational models were retrained or a
supervisor restarted.

The newly versioned aggregate weekly target cannot reuse an existing
weekly-context next-session `1w` model. Each component has its own
horizon-scoped artifact path and compatibility block. There is no multi-output
wrapper, target fallback, or legacy weekly compatibility adapter.

Models are ordinary timestamped artifacts:

```text
DATASTORE/ml/models/<horizon>/<model-name>/
├── <trained UTC timestamp>/
│   ├── model.joblib
│   └── manifest.json
└── latest.json
```

`latest.json` contains a readable relative path and training timestamp. The
checksum in a model manifest is used only to reject a damaged `model.joblib`.
The compatibility block, readable configuration, and checksum are checked before
joblib deserialization.

The manifest also stores an `offline_evaluation` block calculated only after
training and calibration are complete. It records the assessment row count and
date range, explicitly states that assessment was used for neither training nor
calibration, and includes:

- raw and calibrated log loss, Brier score, 0.5-threshold accuracy, and ROC AUC;
- assessment performance for constant training-base-rate and
  calibration-base-rate probabilities;
- prior-period-direction accuracy when available;
- whether calibrated log loss beats each base-rate baseline;
- the calibration partition's raw-probability support and the number of
  assessment raw probabilities below, above, or anywhere outside that support;
  and
- whether the fitted calibrator clips to the observed raw-probability range.

These measurements describe an offline-evaluated candidate. They do not promote
or activate the model.

The manifest's separate `lockbox` block contains only its
`CLOSED_UNTOUCHED_UNSCORED` status, row and target-cluster counts, and
`target_window_start` bounds.

### 4. Generate predictions

Loop B writes:

- `BACKTEST` predictions for the assessment partition;
- fresh ordinary `LIVE` predictions for the latest actionable row per symbol and
  horizon, where `prediction_created_at < actionable_until <=
  target_window_start`; and
- remaining-week `LIVE` predictions where `prediction_created_at <
  actionable_until <= target_window_end`.

If a fresh ordinary row is no longer possible because entry has passed, Loop B
may also carry the latest still-active ordinary forecast for that
`symbol|horizon`. Carry-forward is fail-closed: the original issuance must be a
`LIVE`/`CREATED` row from a verified receipt-era run reachable through the
authoritative publication chain; it must have been created and originally
promoted strictly before `actionable_until`; and its target-definition version,
canonical serialized specification, exact start/end/deadline window, and
round-trip cost must match the current materialized target. The current
publication time must satisfy
`target_window_start <= publication_time < target_window_end`. An orphan,
invalid receipt, `BACKTEST`, post-entry, incompatible, or expired row is never
carried. Selection is deterministic by latest eligible original issuance per
route, and a newer valid fresh row from the current run supersedes it.

A carried row keeps its original `prediction_created_at`, prediction ID,
probability, model version, and target window. Its copy is included in the new
`predictions.parquet` lineage, but receipt-bounded issuance validation excludes
that copy from becoming a new LIVE event. Repeated publications therefore do
not multiply live-evidence counts. Weekly frozen-snapshot reuse remains the
separate policy described below.

For the weekly family, each symbol's latest usable completed daily decision
supplies aggregate `1w` and the contiguous Day 1 prefix whose targets remain in
one exchange week. Aggregate `1w` and `d1` must publish before the first
remaining session closes; later components use their own close. Prior exact
rows may be reused for the same decision, but no prior snapshot or six-route
bundle is required. If no usable group exists, that symbol's weekly LIVE rows
are omitted while the rest of the cycle continues.

Each prediction has:

```text
id = symbol|horizon|decision_timestamp|prediction_created_at
```

The values include:

```text
model_name
model_version
calibration_method
prediction_mode
prediction_status
assumed_round_trip_cost
raw_probability
calibrated_probability
prediction_created_at
```

`model_version` is the readable UTC timestamp directory of the model artifact
used for that row. The pre-existing non-weekly compatibility path can recover
the value from an older verified run directory. Weekly reuse has no such
adapter: every route in the dynamic origin snapshot must carry the current
complete prediction schema.

The runtime does not create IDs for the model, feature set, target, horizon, or
prediction event.

### 5. Run options-strategy analytics

After all required directional routes have produced predictions, Loop B forms
the same closed-lockbox-redacted sample view that will be published and passes
it to `ml.strategy_selection.runtime`. It also supplies every removed
lockbox `target_window_start` as a forbidden set. If any forbidden start
reappears, the strategy stage raises instead of reading, labeling, fitting,
calibrating, assessing, or ranking it.

For each concrete horizon, the strategy stage reconstructs historical
candidates from exact Schwab receipts and computes causal pseudo-outcomes using
future observed BBOs and the modeled $0.65 fee per option contract on each
side. Long legs enter at ask and exit at bid; short legs enter at bid and exit
at ask. Only `COMPLETE` outcomes enter partitions. Wheel and Range-to-Trend
Relay require lifecycle-path evidence and are not assigned fabricated
single-window labels.

Strategy partitioning is chronological by distinct `target_window_start`, and
every candidate in one cluster remains together. The expanding training slice
must retain at least 252 clusters after overlap purging; the following 63
clusters fit Platt calibration; the latest 63 pre-lockbox clusters are
assessment only. Calibration uses inverse decision-size weights so a cluster
with more variants does not dominate. Assessment influences neither estimator
fit nor calibration.

Before fitting, `point-in-time-market-state-v1` combines the separate route
direction probability with causal exact-surface and audited sample context. It
records expected absolute move, expected realized volatility, normalized
uncertainty, trend persistence, and mean-reversion tendency. The exact-mechanics
`greek-bbo-scenario-prior-v2` then evaluates every candidate over deterministic
up/down move scenarios using its aggregate Greeks, holding time, exact BBO
spread, modeled fees, and exact profit/loss bounds. This prior is available on
the first constructible route; it is not an empirical GOOG calibration.

After sufficient complete outcomes exist,
`market-state-hgb-platt-return-v4` fits a nonlinear histogram-gradient-boosting
classifier for profitable outcome and a separate histogram-gradient-boosting
regressor for return-on-risk residual relative to that prior. Platt calibration
is fit only on the calibration partition and only for the probability model.
The fitted route artifact is stored at:

```text
DATASTORE/ml/strategy-models/<horizon>/market-state-strategy-outcome/
├── <trained UTC timestamp>/
│   ├── model.joblib
│   └── manifest.json
└── latest.json
```

Compatibility covers the exact input inventory, decision counts and training
boundary, ordered numeric and categorical features, preprocessing, model,
candidate, outcome, ranking, registry, and research-trace policy versions.
The principal identifiers are `schwab-spreads-strategy-registry-v1`,
`schwab-exact-chain-pricing-candidates-v4`,
`observed-bbo-pseudo-outcome-v2`, `point-in-time-market-state-pricing-v2`,
`pricing-greek-bbo-scenario-prior-v3`,
`pricing-market-state-hgb-platt-return-v5`,
`post-pricing-probability-first-ranking-v4`, `strategy-candidate-v3`, and
`nyu-hu-uh-trace-v3`.

The current candidate pass uses the canonical live directional prediction for
the matching symbol/horizon/decision and publishes every exact-chain variant
that can be constructed from the latest eligible entry receipt. With a fitted
strategy model it writes raw and calibrated profitable-outcome probability,
expected net profit, expected return on risk, calibrated probability as the
primary score, and a probability-first rank. Without a fitted model it uses the
pricing-informed scenario profitable-outcome probability as the primary score,
leaves calibrated probability null, and labels the score basis **Pricing
Scenario**. Each attempted current route also gets
one audit row for each of the 40 registry strategies; missing history or
construction failure is reported there. Even when there are no candidate or
audit rows, the two exact empty schemas are published.

The strategy probability is not the directional probability. The former
estimates whether the exact candidate's observed-BBO net outcome will be
positive; the latter estimates the route's cost-adjusted underlying direction.
Direction instead shapes the sign weights in the causal market-state prior and
enters the fitted strategy model as context alongside move, volatility,
uncertainty, trend, mean-reversion, exact-chain, and audited point-in-time
features. The primary ranking score is profitable-outcome probability; expected
return on risk is a separate secondary key and payoff-magnitude estimate. No
fixed post-calibration direction or account-state bonus is added.
`direction_alignment` remains an auditable measurement. The stage does not
change directional model inputs, targets, partitions, defaults, or artifacts.

The stage writes `strategy-candidates.parquet` and
`strategy-audit.parquet`. It publishes an analytical surface, not an order or
broker instruction. User-controlled ticketing and submission occur later in
the separate Options Strategies tab.

### 6. Reconcile and evaluate matured predictions

Current live predictions are combined with live rows from prior timestamped
runs whose manifests and output inventories verify. Incomplete, damaged, or
unmanifested run directories are skipped. Runs declaring the current
transactional publication contract must also have a valid `publication.json`
receipt bound to the manifest checksum and be reachable through the
authoritative pointer's linked publication history. A complete but unpromoted
working run, including one with an orphaned prepared receipt, therefore cannot
become evidence. An invalid pointer or broken receipt chain aborts
reconciliation fail closed; the runtime never substitutes directory recency.
The combined rows are deduplicated by:

```text
symbol, horizon, decision_timestamp, prediction_created_at
```

They are joined to sample targets using:

```text
symbol, horizon, decision_timestamp
```

Including `horizon` in both grains prevents a `4h` live prediction from
colliding with another horizon at the same symbol and decision timestamp, and
keeps aggregate/Day 1-through-Day 5 weekly evidence independent.

That natural key selects the candidate target row. A prediction is scored only
when all of these conditions hold:

1. prediction status, mode, creation time, and information timing are valid;
2. the natural key resolves to a complete observed target;
3. predicted and observed `target_definition_version` and serialized
   `target_specification` values match;
4. predicted and observed `target_window_start` values match;
5. predicted and observed `target_window_end` values match;
6. persisted `assumed_round_trip_cost` matches the target configuration;
7. a `LIVE` prediction has `prediction_created_at` strictly before
   `actionable_until`, and that deadline does not exceed
   `target_window_start`. All weekly routes share Day 1 open as the deadline.

The only target-contract compatibility exception is a legacy `BACKTEST` row
when both the prediction and observed sample omit both contract fields. A
`LIVE` row always needs the explicit current contract.

The readable reconciliation statuses are:

| Status | Meaning |
| --- | --- |
| `INVALID_PREDICTION` | Prediction status, mode, timestamps, or information timing is invalid. |
| `PENDING` | No complete observed target is available at the natural key. |
| `TARGET_CONTRACT_MISMATCH` | Target-definition version or serialized specification does not match. |
| `TARGET_WINDOW_MISMATCH` | The natural key matched, but target start or end differs. |
| `CONFIGURATION_MISMATCH` | The target window matched, but assumed round-trip cost differs. |
| `POST_ENTRY_PREDICTION` | A live snapshot was created at or after its action deadline. |
| `EVALUATED` | Natural key, target contract, target window, cost configuration, and timing all match. |

Only `EVALUATED` rows receive observed values and score columns. Mismatch and
post-entry rows remain visible for diagnosis but are never scored.

Evaluation values include:

```text
assumed_round_trip_cost
observed_target
observed_forward_raw_return
observed_forward_cost_adjusted_return
raw_log_loss
log_loss
raw_brier_score
brier_score
prediction_correct_0_5
evaluated_at
```

The evaluation `id` uses the same readable four-column grain as its prediction.
When one decision has repeated eligible live snapshots, live performance and
evidence keep the earliest eligible `prediction_created_at` for each
`symbol`, `horizon`, and `decision_timestamp`. Later eligible snapshots remain
in `evaluations.parquet` but do not multiply live evidence.

### 7. Calculate monitoring values

`monitoring.parquet` starts with three global coverage/model rows. Its
`prediction_rows` value covers this cycle's model output and deliberately does
not re-count carried active ordinary rows:

```text
prediction_rows
evaluated_predictions
model_reuse_rate
```

The following evaluated-performance metrics are emitted once at the `global`
scope and once for each `horizon`:

```text
mean_raw_log_loss
mean_log_loss
mean_raw_brier_score
mean_brier_score
accuracy_at_0_5
observed_positive_rate
mean_calibrated_probability
calibration_gap
roc_auc
```

The horizon metrics include evaluated offline assessment rows. When matured
`LIVE` predictions exist for a horizon, the same performance set is also emitted
at the `live_horizon` scope using only those rows.

Each observed symbol/horizon route gets a `completed_live_forecasts` row with
`scope_type = symbol_horizon` and `scope_value = symbol|horizon`. A completed
LIVE forecast is a genuinely prospective `EVALUATED` prediction with a
complete label, compatible contract/window/cost, valid information timing, and
its route-specific deadline geometry: ordinary routes end no later than target
entry, while remaining-week routes end no later than target close. A run declaring the
transactional publication contract must also have a valid `publication.json`
receipt bound to its verified manifest and be reachable through the
authoritative pointer's publication chain. The row counts these forecasts
after deduplication by `symbol`, `horizon`, and `decision_timestamp`; its
`reference_value` is the route's minimum live-evidence threshold:

| Horizon | Minimum completed LIVE forecasts per symbol/horizon route |
| --- | ---: |
| `1h` | 60 |
| `4h` | 60 |
| `1d` | 30 |
| `1w` | 30 |
| `1w-d1` through `1w-d5` | 30 each |

The deliberate initial `4h` threshold is 60 because its decision cadence is
hourly.

Each row is uniquely and readably identified by metric name, scope type, scope
value, and monitoring timestamp. A present value is normally `OK`; a missing
value is `INSUFFICIENT_EVIDENCE`. `completed_live_forecasts` uses the same
`NO_COMPLETED_DECISIONS`, `INSUFFICIENT_LIVE_EVIDENCE`, or
`LIVE_EVIDENCE_AVAILABLE` status as intelligence and the UI.
`calibration_gap` uses a `0.05` absolute-gap reference and becomes `WARNING`
above it. `roc_auc` uses the `0.5` chance reference and becomes `WARNING` below
it. `window_start`, `window_end`, `evidence_row_count`, and `details` state the
evidence behind each value.

### 8. Build current intelligence

In a completed cycle, `intelligence.parquet` contains one row per required
symbol/horizon route. All materialization routes have already passed the
runtime's route-completeness gate at this point. Each row combines the latest
sample, current live prediction, available matured evidence, and readable
current status.

Important columns include:

```text
id
symbol
horizon
decision_timestamp
forecast_created_at
information_available_at
target_window_start
target_window_end
actionable_until
target_definition_version
probability_up
probability_down
actionability_status
operational_status
model_evidence_status
live_evidence_status
intelligence_status
model_name
completed_decision_count
minimum_live_decision_count
automated_action_allowed
limitations
schema_version
```

The `id` recipe is `symbol|horizon|decision_timestamp`. New output uses
`one-id-v2`, which includes the target-definition version and persisted route
threshold. The weekly UI does not synthesize components from an older row.

`completed_decision_count` applies the same unique matured `LIVE` decision rule
to one symbol/horizon intelligence route. It excludes offline assessment
evaluations. Its monitoring row, intelligence row, adapter, and card all use
the same symbol/horizon grain and threshold; pooled `live_horizon` performance
is not this count. The corresponding
`live_evidence_status` is:

| Condition | Status |
| --- | --- |
| no completed LIVE forecasts for the route | `NO_COMPLETED_DECISIONS` |
| some route forecasts, but fewer than its horizon threshold | `INSUFFICIENT_LIVE_EVIDENCE` |
| route count meets or exceeds its horizon threshold | `LIVE_EVIDENCE_AVAILABLE` |

These statuses describe research evidence only. They do not change
actionability, promote a model, or authorize execution.
`automated_action_allowed` remains `false`.

For `1h`, `4h`, and `1d`, a fresh pre-entry forecast is `ACTIONABLE` with
`intelligence_status = RISK_ANALYSIS_SUPPORT`. A verified carried forecast
whose target window is currently active is explicitly non-actionable:

```text
actionability_status = TARGET_WINDOW_STARTED
intelligence_status = FORECAST_IN_PROGRESS
operational_status = OPERATIONALLY_CURRENT
automated_action_allowed = false
```

Its probability and original target window remain present until the strict
target end. Evidence counts and their 60/30 thresholds do not gate this
visibility. A ready non-weekly route with neither a fresh actionable forecast
nor a verified active carried forecast is `OPERATIONALLY_STALE`. A verified weekly route remains
`OPERATIONALLY_CURRENT` while its remaining-week snapshot is published. The
snapshot's aggregate and component session-close deadlines remain explicit,
and a newer completed daily decision may replace it with a shorter outlook.

This output reports evidence and limitations; it does not place trades or change
model settings.

## Run and latest paths

Each completed iteration creates:

```text
DATASTORE/ml/runs/<UTC timestamp>/
├── samples.parquet
├── predictions.parquet
├── evaluations.parquet
├── monitoring.parquet
├── intelligence.parquet
├── strategy-candidates.parquet
├── strategy-audit.parquet
├── manifest.json
└── publication.json
```

No current-state publication is attempted until every required route has
materialized, every required symbol/horizon has at least one prediction, all
seven Parquets have been written, and the manifest has been created. Public
`1w` additionally requires the aggregate and all five components from one new
eligible issuance or one complete verified frozen issuance; partial weekly
success is never published. Every
compatibility mirror is prepared before the authoritative `run.json` pointer
is committed under:

```text
DATASTORE/ml/latest/
├── samples.parquet
├── predictions.parquet
├── evaluations.parquet
├── monitoring.parquet
├── intelligence.parquet
├── strategy-candidates.parquet
├── strategy-audit.parquet
└── run.json
```

The current intelligence compatibility mirror is:

```text
DATASTORE/ml-intelligence/latest/rolling-predictions.parquet
```

`ml/latest/run.json` is the single authoritative current-view commit pointer.
Official readers resolve all seven artifacts from the immutable timestamped run
named there. The predictable Parquets under `ml/latest` and the intelligence
path above are compatibility/convenience mirrors, not an atomic multi-file
snapshot and not publication authority. New pointers use
`current-output-pointer-v1`; receipt-era manifests use
`current-output-authoritative-pointer-v2`.

Loop B durably writes and verifies `publication.json` before the pointer
commit. The receipt identifies the transaction contract, binds to the
completed manifest checksum, and links to the preceding committed receipt-era
publication, if any.
It becomes publication- and evidence-valid only when the atomic pointer makes
it reachable. Route, prediction, staging, receipt, deadline, or pointer-commit
failure leaves the previous pointer authoritative. A failed working directory
can still contain a complete manifest or orphaned prepared receipt, but it is
not reachable through the publication chain and cannot later contribute LIVE
evidence.

A complete successful run with three symbols and public selections `1h 4h 1d
1w` contains 27 intelligence rows: three non-weekly and six weekly rows per
symbol. This is an expected contract count, not a claim that a live 27-row
output has been deployed or observed.

By default the desktop Rolling Forecasts tab follows the authoritative pointer.
An explicit override can read a fixture directly. That tab accepts current
`one-id-v2`, keeps the `1h`, `4h`, and `1d` cards unchanged, and groups the
current aggregate plus Day 1 prefix into one **remaining-week outlook**. It
shows aggregate probability, issuance time, dynamic aggregate bounds, and the
dated remaining components with weekday, UTC/local open/close windows,
probability, and pending/completed evidence status. Rolling Forecasts remains
read-only and keeps automated action disabled.

The sibling Options Strategies tab resolves the same authoritative run's
`strategy-candidates.parquet`, then joins a newly fetched Schwab account
snapshot at display time. It can fill a user-editable ticket only from the
selected **Exact legs** entry and can submit the confirmed component through
`SchwabSession().submit_order(...)`. That UI action is separate from Loop B;
it does not rewrite the immutable publication or turn live holdings into model
features. **Predictive Score** displays the profitable-outcome probability on a
0–100 scale, while **Score Basis** distinguishes **Calibrated ML** from
**Scenario Prior**. It never substitutes the directional forecast. Expected
Return and descriptive Portfolio Fit remain separate and cannot replace or
adjust the published market rank.

The run manifest records a `publication_counts` breakdown with total and
backtest prediction rows, fresh LIVE rows, carried active LIVE rows, retained
frozen-weekly LIVE rows, actionable ordinary routes, and in-progress ordinary
routes. The console reports the same lifecycle categories instead of presenting
one aggregate prediction count as if every current probability were freshly
scored. The manifest may also record run timestamp, input and output paths,
file sizes and integrity checksums, feature columns, target column, model names,
symbols, horizons, route errors, and configuration. It does not assign
identities to any of those values.

For auditability, `1h`, `4h`, and weekly runs serialize the complete readable horizon
specification, target version, target-price source and constituent policy,
calendar-selection policy, processing delay, and cost convention.

## Route errors and current state

Materialization catches source exceptions by symbol/horizon so it can report
which route failed, but `run_loop_b_once` immediately rejects the cycle if any
required route is not `READY`. Model fit/reuse exceptions are likewise recorded
by horizon while the model loop continues, but the subsequent required-route
check rejects the cycle if any requested symbol/horizon has no prediction.
Successful work from another route or horizon is not published as a partial
current run, and no failed-source route is emitted to the UI intelligence file.

Strategy evidence has an explicit analytical degradation path after the
required directional routes succeed. Missing chain history, no eligible entry
or exit receipt, insufficient 252/63/63 clusters, and candidate-construction
failures are recorded in strategy model reports or the 40-row route audit; the
cycle can still publish exact empty candidate/audit schemas or prior-ranked
candidate rows. A schema violation, a reintroduced forbidden lockbox start, or
another uncaught strategy-stage failure still aborts the publication. No
strategy condition silently changes the directional route or partition
requirements.

On either required-route or required-prediction failure, the authoritative
`ml/latest/run.json` pointer continues to name the previous published run. A
source failure occurs before the timestamped run directory is created. A later
failure can leave a timestamped working directory with partial artifacts, or
even a complete manifest but no valid `publication.json` receipt. That
directory is not current and is ignored by later reconciliation.

An unchanged non-weekly route with no fresh candidate first attempts the strict
verified active-forecast carry described above. If none qualifies, it can still
have assessment/backtest predictions but no current forecast. The completed run
then publishes that route in `intelligence.parquet` with null probability,
`NOT_ACTIONABLE`, `OPERATIONALLY_STALE`, and a readable limitation. This is
different from a failed source or missing-prediction route, which aborts
publication.

Because an aborted cycle does not commit the authoritative pointer, the
dashboard continues to load the last successfully published rows and
timestamps; runtime exit status and logs carry the failed-cycle diagnosis.

Deployment, operational model training, and supervisor restart remain operator
actions. This documentation does not claim that a dynamic remaining-week
snapshot was published, does not train against an operational datastore, and
does not restart services.

## Physical Parquet contract

`ml.parquet_contracts` defines explicit Arrow schemas. Every schema:

- begins with one nullable string field named `id`;
- rejects any additional Duckets-generated `*_id`, `*_ids`, hash, digest,
  fingerprint, receipt, or lineage field;
- validates non-empty, unique, readable `id` values;
- rejects hash-shaped and UUID-shaped IDs;
- rejects columns outside the explicit contract.

The Loop A cycle generation and its `WRITING`/`COMPLETE`/`FAILED` status exist
only in the small JSON state file. They are not added to any Parquet schema.
The weekly implementation adds no dataset, Parquet schema column, bootstrap or
backfill workflow, migration adapter, state file, pointer, acknowledgement,
synthetic identifier, or coordination mechanism.

The strategy candidate natural key is
`symbol, horizon, decision_timestamp, candidate_key`; the audit natural key is
`symbol, horizon, decision_timestamp, strategy_name`. `candidate_key` is the
readable strategy/width/front-expiration/back-expiration variant. `legs_json`
stores the complete exact leg graph used for analytics and order drafting, but
it is an ordinary payload value and never a second identity column. Both
strategy schemas are explicit, reject extra columns, and retain exactly the one
leading readable `id`.

The complete schemas and repository-wide provider exception policy are in
[`parquet-id-contract.md`](parquet-id-contract.md).
