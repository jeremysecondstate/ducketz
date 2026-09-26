# Models, forecasts and forward evaluation

Last verified 2026-09-25; code/config authority.

One model runtime consumes completed data snapshots, schedules candidate fits,
publishes forecasts and scores later outcomes. These activities share an
owner but have different clocks. Their implementation does not fetch exchange
data or execute trades. Operator commands and artifact examples remain in the
[component guide](../../hyperliquid-models.md).

## Source map and ownership

| Responsibility | Implementation |
| --- | --- |
| Recipe, horizon and cadence | [Config](../../../configs/hyperliquid-models.json), [loader](../../../ml/hyperliquid_model_config.py) |
| Polling, job scheduling and lifecycle | [Model runtime](../../../ml/hyperliquid_model_runtime.py) |
| Snapshot validation, partitions and estimators | [Models](../../../ml/hyperliquid_models.py) |
| Publication, model loading and forecast journal | [Artifacts](../../../ml/hyperliquid_model_artifacts.py) |
| Duration collection/export | [Timings](../../../ml/hyperliquid_timings.py) |

The verified market config supplies BTC, ETH, HYPE and ZEC, interval and
datastore. Each `(coin, interval, horizon)` has separate fitted estimators,
preprocessing, reports and predictions below `_models/<coin>/15m/h4`.
Shared source code does not mean one pooled cross-coin model.

`_models/_runtime/.runtime.lock` permits one runtime for the datastore. The
owner polls every five seconds and allows one background training job globally
through a one-worker executor. Native numerical threads are bounded to two by
the current config. Polling/inference continues while fitting runs; publication
and collection occur on the polling thread. This is not a guarantee of fixed
five-second end-to-end latency under all I/O or inference loads.

## Configuration and scheduling

Each tick rereads the model and shared market configurations. Valid market
membership changes apply live. Any model-setting change, or a shared interval
or datastore change, requires restart; invalid changes expose `config_error`
while accepted settings remain in use. A removed market's pending fit can
finish but is discarded when collected rather than published. Existing
artifacts remain available for historical review.

The present horizon list is `[4]`: four 15-minute candles, or a one-hour
outcome from each decision candle. Candidate fitting normally becomes due
after 3,600 seconds of source-candle progress since the saved candidate's
input snapshot, with a changed source run. This avoids schedule drift from
fit duration. Older metadata without source-close evidence falls back to
publication age. Missing candidates, changed recipe/feature revision, invalid
candidate timestamps, or explicit bounded force-training can also trigger work.

A round-robin cursor selects due jobs across the configured keys. Failed
training/publication sets a 60-second retry delay in the current config. Model
maximum age is 86,400 seconds for loading, independently of the fitting cadence.
Saved publication time is not the estimator's fitting-data cutoff.

The runtime processes new data/model identities for prediction and scoring;
it does not refit on every poll. A fresh candidate may be published during an
existing candle without replacing the forecast already committed for that
decision close. On stop, the runtime waits for a bounded in-progress fit to
finish, discards its result and stops; it does not publish a half-complete fit.

## Data contract and target semantics

Training pins one completed data run and validates market identity, revision,
ordered feature catalog, row alignment and exact-time outcome consistency.
Only catalog feature names are estimator inputs. Finite, matured future-return
labels are required; fully featureless rows are excluded, while partial
feature missingness is handled by fitting-partition preprocessing.

The binary target is **not-down** when the exact-horizon future return is
nonnegative, and **down** when it is negative. An unchanged future close is
not-down. This deliberately differs from the data pipeline's strict-up label.
Unknown outcomes are not negative examples and cannot enter training as zero.
The forecast exposes `p_not_down` and its complementary `p_down`.

## Chronological evaluation recipe

| Partition | Verified config / boundary | Purpose |
| --- | --- | --- |
| Fitting | At least 1,000 usable rows before calibration | Fit imputer, optional scaler and estimator |
| Calibration | 192 later usable rows | Calibrate each estimator's probabilities |
| Assessment | Last 288 mature usable rows | Measure candidate eligibility; not fit parameters |

At both boundaries, the preceding partition keeps only rows whose target
`label_end_time` is **strictly before** the next partition's first decision
close. Targets ending exactly at the boundary are excluded as well as those
crossing it. Fitting must retain both classes. The report records actual
partition counts, decision cutoffs, label-end cutoffs and purged rows.

All four estimators use median imputation fitted only on the fitting block:

| Member | Current recipe |
| --- | --- |
| Logistic regression | Standard scaling; `C=1`, up to 500 iterations |
| Extra Trees | 128 trees, depth 10, minimum leaf 12, feature fraction 0.7; estimator `n_jobs=1` |
| Histogram gradient boosting | 100 iterations, 15 leaves, learning rate 0.06; no early stopping |
| MLP | Standard scaling; hidden layers 32/16, 100 iterations; no shuffling or early stopping |

Each member receives a later-block logistic/Platt calibration on raw logits
when calibration contains both classes. With one calibration class, it retains
uncalibrated probabilities and records a warning. The ensemble is the equal
mean of the four member probabilities; there are no learned ensemble weights
or online weight adjustments. Convergence warnings are retained in the report.

The exact evaluated bundle is published without refitting on calibration or
assessment rows. Parameters can therefore lag the latest input candle by days
while inference uses its newest feature row. Inspect actual report cutoffs;
do not turn an earlier observed lag into a permanent fixed duration.

## Qualification and publication

Eligibility requires ensemble log loss **and** Brier score to be no worse
than **both** the pre-assessment class-prior baseline and constant `0.5`
baseline, with the implementation's `1e-9` tolerance. The prior is estimated
from matured fitting and calibration outcomes. Accuracy and ROC AUC are
reported but are not the promotion gate.

The assessment block is a promotion holdout, not an untouched final test.
Successive rolling assessments may overlap. Qualification concerns probability
scores; it does not establish trading profitability after fees or funding.
Current coin-specific qualification must be read from current records.

| Artifact | Authority and update contract |
| --- | --- |
| `runs/<model_id>/bundle.joblib` | Exact evaluated fitted bundle |
| `report.json`, `assessment.parquet`, `record.json` | Evaluation, provenance, settings and publication metadata |
| `candidate.json` | Latest completed candidate, eligible or research-only |
| `active.json` | Updated only by an eligible candidate |
| `predictions.parquet` | First recorded forecast for each decision close |
| `latest_prediction.json` | Repairable projection of the latest forecast journal row |
| `outcomes.parquet`, `forward_metrics.json` | Matured saved-forecast outcomes and derived score summaries |

Run artifacts complete before pointer replacement. Failed writes may leave an
unreferenced run; consumers do not select it as a new model. Candidate and
active pointers are separately atomically replaced, not one multi-file
transaction. The loader validates local run identity, version, market,
timestamp age and expected feature revision before loading a bundle.

Inference prefers a fresh eligible active model. Missing or rejected active
metadata, age or feature-revision checks allow selection of a fresh research
candidate. A corrupt bundle that fails deserialization, or an inference
compatibility error, is reported rather than automatically falling through to
the candidate. A newer failed candidate does not displace a still-valid
qualified active model. Forecasts record model role and qualification separately
from later financial outcomes.

## Immutable forecasts and feedback boundaries

Forecast availability must follow a completed decision candle and precede its
still-future target. Time geometry, decision price and complementary finite
probabilities are validated. The Parquet journal commits before the latest
JSON projection. A retry encountering an already recorded decision close
returns its original model/probabilities and repairs the projection if needed.
Model replacement or data correction does not rewrite an earlier forecast.

Scoring waits for the exact target-close price in a later snapshot and for a
forecast created before that target. Missing target candles stay unscored.
Already scored prediction IDs are retained rather than repeatedly rescored.
Forward metrics separate active and research roles, but they do not feed
automatic retraining rules, ensemble weights, promotion gates or paper sizing.

Paper consumes the saved forecast under its own age, quote and policy rules.
The checked-in paper configuration permits research forecasts. Neither a
qualified badge nor a fresh forecast proves that a fill occurred. Four bars
describe the prediction's outcome horizon; paper does not implement a fixed
one-hour holding-period exit. See [Paper](paper.md).

## Failure evidence and maintenance

Runtime status distinguishes configuration, data-loading, prediction and
training errors. A healthy polling heartbeat is not proof of a fresh forecast
or model. Inspect `training_market`, per-key state, pointer times, forecast
availability/target times, feature revision and report cutoffs together.
Timing events distinguish promoted, research-only, failed and discarded jobs.
An event called `training_completed` means collected, not necessarily success.

Use worker time, queue wait, collection/poll wait and publication time as
separate durations. Legacy `last_training_seconds` includes submission-to-
collection waiting; it is not pure fitting time. Journal-write failure is
reported without reclassifying a successfully published model as a failed fit.
See [Timing](../../hyperliquid-timings.md) and [Monitoring](../MONITORING.md).

Relevant checks: [models](../../../tests/test_hyperliquid_models.py),
[artifacts](../../../tests/test_hyperliquid_model_artifacts.py),
[runtime](../../../tests/test_hyperliquid_model_runtime.py), and
[configuration](../../../tests/test_hyperliquid_model_config.py).
Review this page whenever the target, partitions, recipe, qualification gate,
selection priority or immutability boundary changes.
