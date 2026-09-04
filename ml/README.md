# Duckets ML runtime

The `ml` package implements Loop B. It reads current Loop A Parquets, creates
point-in-time samples and targets, trains or reuses ordinary model artifacts,
generates probabilities, evaluates matured predictions, writes monitoring
metrics, and refreshes the rolling forecast view.

Loop B does not fetch providers or mutate Loop A outputs.

## Run Loop B

Run one complete cycle:

```powershell
python -m ml.prediction_runtime `
  --datastore C:\data\duckets `
  --symbols NVDA `
  --provider databento `
  --horizons 1h 4h 1d 1w `
  --once
```

Run continuously by omitting `--once`:

```powershell
python -m ml.prediction_runtime `
  --datastore C:\data\duckets `
  --symbols NVDA GOOG `
  --provider databento `
  --horizons 1h 4h 1d 1w `
  --interval-minutes 30 `
  --phase-offset-minutes 6 `
  --failure-retry-attempts 1 `
  --failure-retry-delay-seconds 60 `
  --stale-recovery-minutes 35
```

`1w` is the public selection for one weekly family. The runtime expands it to
the aggregate `1w` route and `1w-d1`, `1w-d2`, `1w-d3`, `1w-d4`, and `1w-d5`;
operators do not list those internal values manually. The public behavior of
`1h`, `4h`, and `1d` is unchanged.

The supervisor creates `.duckets-ml-prediction-runtime.lock` in the datastore.
A second Loop B process fails before doing work. `Ctrl+C` stops cleanly and
removes the lock. A later supervisor may replace the lock only when its recorded
PID is positively confirmed dead; live, missing, malformed, or unqueryable
ownership remains fail-closed.

Production runs at `:06`/`:36`. One retry is permitted only for a classified
transient failure, and a restarted supervisor runs immediately when its last
receipt-verified publication has been authoritative for at least 35 minutes.
Deadline, integrity, and deterministic contract failures remain fail-closed;
the prior verified publication stays authoritative.

Useful runtime choices are:

```text
--feature-profile loop-a-all-v1|production-v1|technical-all-v2|loop-a-all-bsgp-shadow-v1|loop-a-all-bsgp-active-v3
--model-family logistic|lightgbm|xgboost
--calibration none|platt|isotonic
--balanced-class-weight
--minimum-train-clusters N
--calibration-clusters N
--assessment-clusters N
--lockbox-clusters N
--round-trip-cost RATE
```

Strategy selection is not a Loop B stage. `ml.strategy_runtime` consumes an
already-published authoritative Loop B run, OPRA-first point-in-time option
history with verified Schwab snapshot fallback, stock BBO evidence, and active
Pricing evidence, then publishes a separate immutable Strategy run. It
can lag or retry without delaying directional predictions and never mutates the
source Loop B directory:

```powershell
python -m ml.strategy_runtime --datastore C:\data\duckets --once
```

Loop B and Options Strategies also record a non-authoritative coverage report
from the pooled causal sequence encoder when a checksum-valid shadow
publication is available. The shared encoder is independently published,
cannot change either production ranking, and feeds only the observe-only Loop C
risk engine. See the
[pooled sequence and Loop C contract](../docs/loops-system-analysis/POOLED_SEQUENCE_LOOP_C.md).

See the [Strategy runtime audit](../docs/loops-system-analysis/loops/strategy-runtime.md)
and [Loops relationship map](../docs/loops-system-analysis/LOOP_RELATIONSHIPS.md).

For `4h`, the closed profile routes resolve to `technical-all-4h`,
`technical-all-v2-4h`, and `loop-a-all-v1-4h`. The default set is an exact
ordered 69-value clone of `loop-a-all-v1-1h`, with the same family composition
but a distinct horizon-scoped semantic contract.

A target cluster is one distinct `target_window_start` value and contains every
sample with that value. The `1h` and `4h` routes therefore have multiple
clusters in a calendar day. When these four cluster-count options are omitted,
each horizon uses its own defaults:

| Horizon | Minimum training clusters | Calibration clusters | Assessment clusters | Closed lockbox clusters |
| --- | ---: | ---: | ---: | ---: |
| `1h` | 252 | 63 | 63 | 126 |
| `4h` | 252 | 63 | 63 | 126 |
| `1d` | 252 | 63 | 63 | 126 |
| `1w` | 252 | 63 | 63 | 126 |
| `1w-d1` through `1w-d5` | 252 | 63 | 63 | 126 |

Supplying one of the options overrides that count for every selected horizon.
Partition purging follows the materialized target geometry, not the horizon
name. If target windows overlap, rows are purged at
training-to-calibration, calibration-to-assessment, and
assessment-to-lockbox boundaries; enough earlier clusters must remain to
satisfy the requested partition sizes. Windows that end exactly when the next
one starts are conservatively treated as sharing a target-price endpoint and
are purged; their labels are not available before that boundary. Target starts
that already have a verified prior `LIVE` prediction are excluded from offline
partitioning, so a genuinely post-training outcome can mature as live evidence
without being reclassified into the closed lockbox. The latest remaining
completed clusters form that model-time lockbox. Its targets are never coerced,
read, returned in `samples.parquet`, predicted, or scored; model JSON records
only `CLOSED_UNTOUCHED_UNSCORED`, row and target-cluster counts, and
`target_window_start` bounds.

Use either `--datastore PATH` or `--datastore-target pc|local`.

## Cycle stages

One Loop B iteration:

1. discovers or accepts symbols;
2. reads adjusted price bars and the registered Loop A feature Parquets,
   including technical, fundamental, lifecycle, quote, option, energy, macro,
   SEC, and CME families;
3. validates the explicitly registered feature columns;
4. combines features by symbol and timestamp;
5. constructs `1h`, `4h`, and `1d` target windows plus the aggregate and five
   component weekly target windows using the exchange calendar;
6. partitions completed samples chronologically by `target_window_start`
   cluster into training, calibration, assessment, and a latest closed lockbox,
   purging target-window overlap and never reading lockbox targets; the bounded
   100-calendar-day minute input uses 160/40/40/80 clusters for `1h` and
   128/32/32/64 for the lower-frequency, overnight-overlapping four-checkpoint
   `4h` route,
   while daily and weekly routes retain 252/63/63/126;
7. reuses a compatible model or fits and calibrates a new model;
8. writes the point-in-time sample view, including complete and not-yet-mature
   labels but omitting exact closed-lockbox rows;
9. writes backtest and strictly pre-entry fresh live predictions, then carries
   at most one still-active ordinary forecast per route from the verified
   authoritative receipt chain when no newer valid current-run forecast exists;
   carried rows keep their original issuance and exact target/cost contract;
10. reconciles predictions to matured targets by natural key, exact target
    window, and the same cost configuration;
11. records `TARGET_WINDOW_MISMATCH`, `CONFIGURATION_MISMATCH`, or
    `POST_ENTRY_PREDICTION` without scoring, and calculates row-level metrics
    only for eligible matches;
12. calculates global coverage and model-reuse metrics, plus evaluated
    performance at global and per-horizon scopes;
13. calculates pooled `live_horizon` performance when matured LIVE predictions
    exist and separately counts completed LIVE forecasts for each
    symbol/horizon route against that route's horizon threshold;
14. writes monitoring values and one current intelligence row per
    symbol/horizon; carried ordinary rows are `TARGET_WINDOW_STARTED` plus
    `FORECAST_IN_PROGRESS`, never `ACTIONABLE`, and are excluded from fresh
    prediction coverage while remaining in publication lineage;
15. refreshes compatibility mirrors and atomically commits the authoritative
    current-run pointer. Only after this point can the independent Strategy
    runtime consume the run.

The versioned intraday target contracts are
`next-60-eligible-equity-minutes-open-close-v6` for `1h` and
`next-180-eligible-equity-minutes-four-checkpoints-v4` for `4h`. Both use
completed canonical `1h` bars as the decision and feature source and may select
the latest completed full regular or standard US extended source hour between
04:00 and 20:00 Eastern. Native Databento rows win duplicate timestamps, while
a coverage-proven derived hour fills native publication gaps. A successful
Databento `1m` selected range permits sparse trade-bearing aggregation and a
strictly prior-close, zero-volume mark for a wholly empty completed hour; no
future trade, partial trailing hour, holiday, weekend, or out-of-envelope hour
is synthesized. Without explicit provider-range proof, the derived lane keeps
the strict all-60-minute rule.

Source context and broker-actionable targets have separate policies. On an
ordinary US equity day, target minutes are PRE 07:00--09:25, REGULAR
09:30--16:00, and POST 16:05--20:00 Eastern. The five-minute transitions,
weekends, holidays, and closed periods pause accumulation. Early-close days do
not receive synthetic extended segments. Non-US calendars retain their prior
regular-session hybrid behavior.

The `1h` route contributes each actionable segment start and each eligible
exchange-local clock-hour start. Its 60 eligible target minutes may pause over
a closed transition gap, preserving the distinct 09:00 Eastern target. Each
source hour may also emit bounded target siblings through two hours after its
information-availability timestamp so a one-source-hour publication lag does
not erase the next exact hourly target; a newer source row still wins when it
exists. The `4h` route contributes
only 07:30, 11:30, 15:30, and 19:30 Eastern—04:30, 08:30, 12:30, and 16:30
Pacific. The first candidate strictly after `information_available_at` wins;
equality is too late. The 19:30 Eastern checkpoint accumulates 30 POST minutes,
pauses overnight, consumes the next PRE segment, pauses the 09:25--09:30 gap,
and normally reaches its 180th eligible minute at 09:35 Eastern the next
eligible day.

Target marks use canonical adjusted native Databento `1m`, version
`canonical-adjusted-native-1m-causal-no-trade-marks-v2`. Databento emits
trade-bearing OHLCV only for non-empty minutes. A selected minute therefore
uses its native open/close when present or the latest strictly prior native
close when no trade bar exists. It is never backfilled from a future row, and
collection coverage must extend through the target end before the label can
mature. `target_open` is the first selected mark and `target_close` is the final
selected mark. Target values do not enter the feature matrix, and
`previous_period_direction` continues to come from the completed canonical
`1h` source bar.

`target_window_start` and `actionable_until` are the selected start,
`target_window_end` follows the 60th or 180th eligible minute, and
`label_available_at` is that end plus five minutes. The raw return receives the
configured round-trip-cost subtraction exactly once, and the positive class is
strictly greater than zero. The official regular-session definition remains
unchanged for options, Pricing, Strategy, daily, and weekly contracts.

The weekly family creates historical rolling candidates after every eligible
daily decision so each of its six single-target models retains daily training
density. `1w-d1` through `1w-d5` respectively use each of the next five
eligible sessions' official open and close. Aggregate `1w` starts at D+1's
official open and ends at the final eligible close of D+1's exchange week. The
configured round-trip cost is subtracted once from each route's simple return.
Daily components mature independently at their session close plus processing
delay; aggregate `1w` matures after its dynamic final close plus that delay.

The aggregate target version is
`dynamic-remaining-week-aggregate-open-close-v2`; component target versions
are `dynamic-remaining-week-d1-open-close-v2` through
`dynamic-remaining-week-d5-open-close-v2`. These versions prevent reuse of the
retired fixed-window targets.

LIVE issuance follows each symbol's latest usable completed daily decision.
The exchange calendar, not fixed weekday or UTC arithmetic, chooses the
remaining sessions and handles holidays, early closes, weekends, and DST.
Aggregate `1w` and `1w-d1` expire at the first remaining session close; each
later component expires at its own close. A Tuesday-onboarded symbol can issue
Tuesday-through-Friday while a Wednesday-onboarded symbol independently issues
Wednesday-through-Friday. Repeated cycles may reuse exact published rows for
the same decision, but prior publication is not required to issue.

If a symbol has no usable aggregate-plus-component prefix before its applicable
close, that symbol's weekly LIVE output is empty for the cycle. The runtime
continues with other symbols and routes and tries again from the next completed
daily decision.

Live-evidence thresholds are 60 decisions for `1h`, 60 for `4h`, and 30 for
`1d` and each of the six internal weekly horizons. Offline assessment rows
contribute to evaluated performance
metrics but never to the live-evidence count. A completed LIVE forecast is a
genuinely prospective, matured, contract/window/cost-compatible prediction
recovered from a complete verified run; runs declaring the transactional
publication contract must also have a valid `publication.json` receipt and be
reachable through the authoritative pointer's publication chain.
Counts are route-specific by symbol and horizon. Repeated live snapshots for
one symbol/horizon/decision are canonicalized to the earliest eligible
pre-entry prediction before live performance or evidence counting. Pooled
`live_horizon` rows summarize model performance only and are not a card's
evidence count. Meeting a threshold changes the readable evidence status only;
automated action remains disabled.

The Rolling Forecasts UI also reads the receipt-verified sibling
`evaluations.parquet` and shows descriptive route performance immediately below
Live Evidence. `Cumulative Live` uses every unique compatible matured decision.
`Rolling` uses the most recent route threshold (60 decisions for `1h`/`4h`, 30
for daily and weekly routes), or all available decisions while that window is
still filling. Both views show hit rate, the Down-only baseline, and lift over
that baseline. The tab refreshes these read-only values hourly; they change only
when a new outcome has causally matured. These display metrics do not authorize
model changes, promotion, automated action, or orders.

A model error is diagnosed at its horizon. By default, valid routes still
publish and limitations remain visible; `--require-all-routes` opts into strict
whole-cycle rejection. Selecting public `1w` materializes the six internal
historical model routes, but LIVE issuance needs only the aggregate and the
same-week Day 1 prefix applicable to that individual symbol at that moment.

## One-ID Parquet contract

Every Loop B Parquet starts with exactly one Duckets-generated string column
named `id`. IDs are readable natural keys:

| Output | `id` recipe |
| --- | --- |
| samples | `symbol\|horizon\|decision_timestamp` |
| predictions | `symbol\|horizon\|decision_timestamp\|prediction_created_at` |
| evaluations | `symbol\|horizon\|decision_timestamp\|prediction_created_at` |
| monitoring | `metric_name\|scope_type\|scope_value\|monitored_at` |
| intelligence | `symbol\|horizon\|decision_timestamp` |

The files contain readable columns such as `model_name`, `horizon`,
`prediction_mode`, `assumed_round_trip_cost`, `label_status`,
`evaluation_status`, and `schema_version`. They do not contain model,
feature-set, target, policy, route, publication, receipt, or lineage IDs.

Exact schema examples and enforcement live in
[`ml/parquet_contracts.py`](parquet_contracts.py) and the corresponding
publication/runtime tests.

## Timestamped artifacts

Each completed cycle writes:

```text
DATASTORE/ml/
├── runs/
│   └── <UTC timestamp>/
│       ├── samples.parquet
│       ├── predictions.parquet
│       ├── evaluations.parquet
│       ├── monitoring.parquet
│       ├── intelligence.parquet
│       ├── manifest.json
│       └── publication.json
└── latest/
    ├── samples.parquet
    ├── predictions.parquet
    ├── evaluations.parquet
    ├── monitoring.parquet
    ├── intelligence.parquet
    └── run.json
```

Strategy uses a separate topology and authority:

```text
DATASTORE/ml/
├── strategy-runs/<UTC timestamp>/
│   ├── strategy-candidates.parquet
│   ├── strategy-audit.parquet
│   ├── strategy-model-reports.json
│   ├── manifest.json
│   └── publication.json
└── strategy-latest/run.json
```

Timestamp directory names use UTC, for example
`20260729T184512.123456Z`. A numeric suffix resolves the unlikely case of two
runs sharing the same microsecond.

`manifest.json` is a small readable record of run time, input files, output
files, feature columns, target column, models, symbols, horizons, route errors,
and configuration. Output checksums verify file integrity only. They are never
artifact names, row values, or join keys.

`publication.json` binds a successfully published run to its manifest
checksum and is required when that run is later considered for LIVE evidence.
Each receipt links to the prior committed receipt-era publication, if any. A
failed working directory can contain a complete manifest, or even an orphaned
prepared receipt after a process interruption, but it is excluded unless it
is reachable through the authoritative pointer's verified chain.

`ml/latest/run.json` is the single authoritative current-view commit pointer.
Official readers resolve all five immutable Parquets from the timestamped run
path named there. The predictable Parquets under `ml/latest` and
`ml-intelligence/latest/rolling-predictions.parquet` are compatibility and
convenience mirrors, not publication authority.

## Model fit and reuse

Model artifacts use readable hierarchy and timestamped directories:

```text
DATASTORE/ml/models/
└── <horizon>/
    └── <model-name>/
        ├── <trained UTC timestamp>/
        │   ├── model.joblib
        │   └── manifest.json
        └── latest.json
```

For example, the default model name for the one-day route is
`logistic-1d`.

Loop B reuses the latest model only when its readable manifest matches the
requested model family, horizon, feature columns, target column, calibration
method, class weight, input file metadata, row counts, and training-through
timestamp. It must also match the Python implementation and major/minor version,
plus recorded versions of NumPy, pandas, PyArrow, scikit-learn, joblib,
exchange-calendars, LightGBM, and XGBoost. Compatibility and checksum checks
happen before `model.joblib` is loaded. A mismatch causes a new fit in a new
timestamp directory.

The `1h` and `4h` manifests store the complete readable horizon specification,
target-definition version, target-price provider/timeframe/source version and
constituent rule, calendar-policy version and definition, processing delay,
one-time cost convention, assumed cost, and strict-positive classification.
The v2 intraday contracts therefore invalidate legacy `1h` and `4h` model
reuse without changing `1d` compatibility. Each of the six weekly model
manifests stores its readable horizon specification, target-definition version,
and assumed round-trip cost. The new aggregate target version prevents an
existing weekly-context next-session `1w` model or prediction from being reused;
the five component horizon names and target versions are likewise distinct.
There is no compatibility fallback or legacy adapter for the retired weekly
target.

The aggregate `1w` logistic route uses an L1-regularized fit with `C=0.3`, the
`liblinear` solver, and a `5,000`-iteration ceiling. Its Platt calibrator uses
`C=0.1`; before calibration, raw probabilities are bounded to the minimum and
maximum raw scores observed in the chronological calibration partition. This
prevents the calibrator from extrapolating beyond the data on which it was fit.
These settings apply only to aggregate `1w`; the five `1w-d1` through `1w-d5`
component models retain the default logistic and calibration settings. Both
sets of parameters are recorded in the aggregate model manifest and therefore
invalidate reuse of an aggregate artifact trained under the old settings.

The model manifest records offline assessment evidence separately from fitting:
assessment clusters are used for neither training nor calibration. It stores
raw and calibrated log loss, Brier score, 0.5-threshold accuracy, and ROC AUC;
training-base-rate and calibration-base-rate comparisons; prior-period
direction accuracy when available; and whether calibrated log loss beats each
base-rate baseline. It also records the calibration partition's raw-probability
range and the number of assessment predictions outside that range.

The same manifest records no lockbox target values, predictions, scores, or
metrics. Its lockbox block contains only the
`CLOSED_UNTOUCHED_UNSCORED` status, row and target-cluster counts, and
`target_window_start` bounds.

`latest.json` is a path pointer, not a model identity registry. The model
checksum protects `model.joblib` from corruption and has no semantic role.

## Current rolling output

After writing a run, Loop B maintains a compatibility mirror at:

```text
DATASTORE/ml-intelligence/latest/rolling-predictions.parquet
```

By default the UI resolves the authoritative `ml/latest/run.json` pointer and
reads `intelligence.parquet` from that immutable run. The path above remains a
compatibility mirror for existing non-weekly consumers. New output uses physical
schema `one-id-v2`. Each row is unique by `symbol`, `horizon`, and
`decision_timestamp`. Version 2 persists the target definition and
route-specific live-evidence denominator; the visible card cleanup alone did
not require or cause the schema change.
For `1h`, `4h`, and `1d`, `OPERATIONALLY_CURRENT` means the route has an
actionable fresh prediction or a receipt-proven carried forecast whose target
window is currently in progress. The latter retains its original probability
and window but is explicitly non-actionable. A ready non-weekly route with
neither state is `OPERATIONALLY_STALE`. A verified remaining-week route remains
current for its published decision. Aggregate `1w` and `d1` use the first
remaining session close as their deadline; later components use their own
session closes.

The UI follows the authoritative pointer; it does not discover a run by
directory recency. See the
[system functionality audit](../docs/loops-system-analysis/SYSTEM_FUNCTIONALITY.md).

A successful publication contains one route for every requested internal
symbol/horizon. With `GOOG`, `MU`, and `NVDA` and public selections `1h 4h 1d
1w`, that means 27 current-output rows: three unchanged non-weekly routes plus
six weekly model-route rows per symbol. The UI groups the aggregate and current
Day 1 prefix into one **remaining-week outlook** and treats the unused later
component rows as unavailable rather than as forecasts. This is a
contract consequence, not a claim that such a live publication has been
deployed or observed; deployment and supervisor restart remain operator actions.

## Focused documentation

- [Directional Loop B runtime](../docs/loops-system-analysis/loops/directional-loop-b.md)
- [Production loop map and timing](../docs/loops-system-analysis/LOOP_MAP.md)
- [Loop relationship catalog](../docs/loops-system-analysis/LOOP_RELATIONSHIPS.md)
- [System functionality and publication contracts](../docs/loops-system-analysis/SYSTEM_FUNCTIONALITY.md)
- [Strategy runtime](../docs/loops-system-analysis/loops/strategy-runtime.md)
