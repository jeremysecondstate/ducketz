# Hyperliquid models and continuous predictions

For the maintained cross-system map, runtime inventory and operating procedures,
see [Hyperliquid system analysis](hyperliquid-system-analysis/README.md).

For persistent worker/publication timings, polling-delay separation and timing
exports, see [Hyperliquid operation timing](hyperliquid-timings.md).

This is the model side of the shared Hyperliquid system. It consumes the
completed OHLCV, feature, and label snapshots produced by the existing data
coordinator. All symbols use one training and prediction implementation, with
separate fitted models and evaluation records for each symbol and horizon.
It does not submit orders, move funds, or import Schwab execution or model
publication runtimes. The existing public-data coordinator continues running
independently.

The live configuration uses chronological **70% fitting, 15% calibration, and
15% assessment** as approved on September 26, 2026. The production ensemble
still contains the same four model families. Expanded classical and compact
sequence models are evaluated by a separate offline research command; they
are not active production members.

The September 28 frequency experiment uses **5-minute candles and a one-bar,
next-5-minute forecast**, with candidate fitting due every 300 seconds of
source-candle progress. The retained 15-minute datasets and dated results remain
historical evidence. Indicator windows still count candles; they were not
rescaled to preserve their former elapsed-time spans.

The September 28 Paper improvement trial adds explicit positive estimator
weights and configurable calibration regularization. Its selected settings are
`calibration_c=0.1` and normalized weights logistic 0.4, ExtraTrees 0.2,
HistGradientBoosting 0.2 and MLP 0.2. Base estimator architectures, MLP's
100-iteration budget, chronological 70/15/15 splits and qualification criteria
remain unchanged. The weights are saved in each bundle and used consistently
for assessment and forecasting; older bundles retain their equal mean.
Omitted settings preserve the previous calibration C=1 and equal weighting.
All weights must remain positive and finite.

Isolated development selection and later confirmation favored this candidate
on average probability losses and a target-change proxy. The confirmation
proxy fell about 14.6%; it is not measured live turnover or proof of profitability.
The next three-day Paper run is the prospective test. See the
[improvement workflow](hyperliquid-system-analysis/PAPER_IMPROVEMENT.md) and its
dated audit for actual deployment/receipt status rather than treating this
description as evidence that a worker is running.

## First measured run

This section records the original **fixed-row** recipe, before the later
70/15/15 configuration change. Its timings, qualification results, 192/288
reserved blocks and 123-hour fitting lag are historical measurements.

On 2026-09-26 at 01:36 UTC, the bounded run fitted all four model types for
BTC, ETH, HYPE, and ZEC at the one-hour horizon: 16 primary estimators using
38 features and approximately 5,000 completed input candles per market. The
complete runtime pass, including calibration, assessment, saving artifacts,
and predictions, took approximately **7.48 seconds** on this machine with the
configured two-thread numerical limit.

| Market | Candidate build time from its report | Initial assessment result |
| --- | ---: | --- |
| BTC | 2.608 seconds | Research candidate; did not qualify against both baselines |
| ETH | 1.426 seconds | Qualified against both baselines |
| HYPE | 1.364 seconds | Research candidate; did not qualify against both baselines |
| ZEC | 1.348 seconds | Qualified against both baselines |

These are measurements of this fixed recipe and dataset, not a comparison
against the complete older BERA model set. All four MLP fits reached their
configured 100-iteration budget with a convergence warning recorded in the
report; the measured speed does not establish full numerical convergence.
Qualification here concerns retrospective probability scores, not demonstrated
trading profitability.

The snapshot's latest candle closed at `2026-09-26T01:30:00Z`, while the
estimators' fitting data ended at `2026-09-20T22:30:00Z`: **123 hours earlier**.
That deliberate gap comes from holding out 192 calibration rows, 288 assessment
rows, and the horizon-boundary exclusions. The evaluated bundles are published
without refitting on those reserved rows. They predict from fresh feature rows,
but their estimator parameters have not been fitted to the latest five days.
The timing result makes faster retraining feasible; it does not remove this
evaluation-policy lag. Each saved report retains the exact cutoffs and timings.

## Run and inspect

From `C:/dev/ducketz` in PowerShell:

```powershell
& .\.venv\Scripts\python.exe -m ml.hyperliquid_model_runtime --once
```

`--once` runs a bounded pass over the currently configured markets and
horizons, fitting candidates that are due and saving predictions before
exiting. Existing fresh candidates are reused. Add `--force-train` to fit each
configured symbol/horizon once even when a recent candidate exists. The run
reports include measured training times, useful for checking the initial model
output before choosing a longer-running retraining cadence.

For continuous operation:

```powershell
& .\.venv\Scripts\python.exe -m ml.hyperliquid_model_runtime
& .\.venv\Scripts\python.exe -m ml.hyperliquid_model_runtime --status
& .\.venv\Scripts\python.exe -m ml.hyperliquid_model_runtime --stop
```

The latter two commands can be run from another terminal. `--config PATH`
selects another model configuration. Status and stop controls use the default
data root without requiring valid model configuration; for a custom datastore,
pass `--output-root PATH` to the control command. No Windows startup task is
installed by these commands.

## Shared universe and configuration

The checked-in model settings are in `configs/hyperliquid-models.json`:

```json
{
  "version": 1,
  "markets_config": "hyperliquid-markets.json",
  "horizons_bars": [1],
  "retrain_seconds": 300,
  "poll_seconds": 5,
  "retry_seconds": 60,
  "model_threads": 2,
  "min_train_rows": 1000,
  "split_mode": "fractions",
  "train_fraction": 0.70,
  "calibration_fraction": 0.15,
  "assessment_fraction": 0.15,
  "max_train_rows": null,
  "max_model_age_seconds": 86400,
  "calibration_c": 0.1,
  "logistic_weight": 0.4,
  "extra_trees_weight": 0.2,
  "hist_gradient_boosting_weight": 0.2,
  "mlp_weight": 0.2
}
```

`markets_config` resolves relative to the model configuration's directory.
That existing market configuration supplies the symbol list, candle interval,
and data root. Add or remove symbols there; do not duplicate the universe in
the model config or copy Python folders. The model runtime rereads market
membership while running. A removed market's training may finish, but the
runtime does not publish that result after the removal has been applied.

The current horizon is one candle: the next five minutes for the `5m`
dataset. Other configured horizons must have corresponding future-return
columns in the input labels. The data pipeline provides horizons of 1, 4 and
16 candles by default, which mean 5, 20 and 80 minutes on this input.
A forecast describes its outcome window, not a guaranteed trade or forced exit.

The current retraining cadence is **every five minutes** (`retrain_seconds=300`).
A new next-five-minute prediction is recorded for each completed five-minute
candle. Retraining follows elapsed candle
history since the candidate's input snapshot and requires a changed source
run, so fitting duration does not gradually move the schedule later. This is
an explicit configuration override; the loader's fallback default remains
3,600 seconds when the setting is omitted.
Changing training settings requires a runtime restart; symbol membership
continues to come from the shared market config.

### Chronological fraction controls

Fractions apply to **mature usable rows**, after excluding unavailable
exact-horizon outcomes and rows without any usable feature. They are not
percentages of every raw candle. For `N` such rows, the allocator takes
`floor(0.70 * N)` for fitting and `floor(0.15 * N)` for calibration, and assigns
the remaining rows to assessment. These are nominal chronological blocks.
All three configured fractions must be finite, strictly between zero and
one, and sum to one.

Each preceding block then retains only targets whose `label_end_time` is
**strictly before** the next block's first decision close. Labels ending at
that boundary are removed too. Purging does not borrow replacement rows from
another block. The optional fitting-window cap is applied afterward, so actual
fractions can be below the requested fractions and need not sum to one.
Preprocessing is fitted only on the surviving fitting rows.

For example, the September 26 pinned research snapshot had 5,073 mature
usable BTC rows. Its nominal allocation was 3,551 / 760 / 762. Four rows at
each horizon boundary were purged, yielding 3,547 / 756 / 762 before any
sequence-context filtering. This is approximately 69.92% / 14.90% / 15.02%
of the original denominator. Exact counts change with available history.

| Report field | Meaning |
| --- | --- |
| `split_mode` | `fractions` for the configured live policy |
| `mature_usable_rows` | Denominator before boundary purging or a fitting cap |
| `requested_split_fractions` | Requested 70/15/15 proportions |
| `requested_split_rows` | Nominal allocation after rounding |
| `effective_split_rows`, `effective_split_fractions` | Surviving rows and fractions of the same denominator |
| `boundary_purged_rows`, `purged_rows` | Strict horizon exclusions by boundary and in total |
| `splits` | Actual decision and label-end cutoffs for each block |

The loader and Python settings retain `split_mode="fixed_rows"` as a
compatibility default when the fraction controls are omitted. That older
mode reserves 192 calibration and 288 assessment rows by default. Supplying
fraction values in fixed-row mode is rejected; fixed-row counts do not control
allocation in fraction mode. The live JSON explicitly opts into fractions.

### Optional fitting-window cap (not enabled)

`max_train_rows` is an available configuration field, but it was **not enabled**
after the September 26 offline comparison. Production remains uncapped under
the chronological 70/15/15 policy shown above. Omitting the field
or setting it to JSON `null` selects the Python default `None` and keeps all
eligible fitting rows. A configured cap must be an integer at least
`min_train_rows`; booleans, fractional values and smaller caps are rejected.

The implementation first constructs the chronological assessment/calibration
blocks and purges overlapping label horizons. It then removes only the oldest
rows of the remaining fitting block, retaining at most `max_train_rows` recent
rows. Calibration and assessment rows are unchanged. Minimum fitting size and
the presence of both target classes are checked after the cap. The cap does not
move the fitting end time forward or shorten the reserved-window fitting lag.
Its value is included in the saved model settings that identify the recipe.

New reports distinguish recency-window omissions from horizon exclusions:

| Report field | Meaning |
| --- | --- |
| `max_train_rows` | Requested cap, or `null` for uncapped fitting |
| `fit_rows_before_window_cap` | Mature fitting rows available after horizon purging, before the optional cap |
| `training_window_omitted_rows` | Old fitting rows removed by the cap; zero when uncapped |
| `purged_rows` | Rows excluded at label-horizon partition boundaries; excludes window omissions |
| `splits.fit.rows` | Rows actually used to fit the estimators and their preprocessing |

See [model settings and splitting](../ml/hyperliquid_models.py) and
[configuration validation](../ml/hyperliquid_model_config.py). This capability
does not select or activate a different production training policy by itself.

## Work scheduling and model recipe

The later [September 26 regularization and iteration-budget comparison](hyperliquid-system-analysis/audits/2026-09-26-training-regularization-and-budget.md)
also retained these training settings. Stronger regularization, removing the
neural member from that regularized ensemble, and raising only its iteration
limit from 100 to 300 each worsened both selection losses. No tested training
parameter from those two experiments was adopted. The later approved change
sets the chronological split to 70/15/15 while preserving all four estimator
recipes. For the earlier Paper restart and wider ordinary rebalance threshold,
see the
[fresh-run audit](hyperliquid-system-analysis/audits/2026-09-26-paper-retune-fresh-mirror.md).

The runtime polls for completed data snapshots every five seconds. Predictions
use an already saved model. New model fits run in a background worker thread,
with at most one training job globally and two numerical threads by
default. Training does not run inside the data-fetching process or block the
runtime's inference polling. There is no need to refit on every polling tick.

The current production ensemble contains logistic regression, Extra Trees,
histogram gradient boosting, and a small multilayer perceptron with hidden
layers of 32 and 16 units. Each symbol/horizon has its own fitted instances.
Generic implementation code is shared; fitted scalers, parameters, and
predictions are not shared between symbols or with Schwab models.

An input snapshot is selected once for each training run. Features and labels
come from that same immutable run. Feature names are taken from its feature
catalog, so target columns and raw metadata are not silently added as inputs.
Unmatured outcomes are not negative training examples.

The target is **not-down versus down**: a nonnegative exact-horizon future
return is class 1, and a negative return is class 0. Thus an unchanged future
price belongs to not-down. The ensemble's not-down probability is paired with
`1 - probability_not_down` for down. These labels are derived from the saved
future-return column; the data pipeline's existing strictly-up label columns
have different tie semantics and are not relabeled silently.

## Evaluation and promotion

Data is partitioned chronologically into fitting, calibration, and later
assessment periods. Partition boundaries respect each target's maturity time:
the original candle close plus the horizon duration. Outcomes that cross a
boundary are excluded from the preceding partition. Model preprocessing is
fitted only on the fitting partition. Calibration uses its own later period;
the assessment outcomes are not used to fit either the estimator or calibrator.

The saved candidate is the exact evaluated bundle. Version one does not refit
that bundle on the assessment rows after scoring. Consequently, the estimator's
fitting cutoff is older than the latest completed candle; the run report makes
that lag visible along with the calibration cutoff. A future refitting policy
would need its own evaluation design.

At roughly 5,000 mature five-minute rows, each 15% reserved block is about
750 rows, or 2.6 days. The September 28 `5m/h1` feasibility snapshot had a
**125.17-hour fitting lag**, about 5.2 days. Five-minute retraining does not
fit the base estimators on the latest five days of outcomes; those rows remain
reserved for calibration and assessment. The historical September 26
15-minute fractional comparison had a 382.5-hour lag, while the earlier
fixed-row example above had a 123-hour lag. These are snapshot values;
inspect
`model_fit_lag_hours`, `model_fit_through_close_utc` and
`calibration_through_close_utc` in each report.

Each candidate is saved whether it qualifies or not. Promotion to `active.json`
requires its assessment log loss and Brier score to qualify against both the
pre-assessment class-prior baseline and the constant 0.5 baseline. That prior
uses only matured fitting and calibration outcomes. A failed candidate does
not replace a qualified active bundle. When there is no qualified active model,
the runtime can still publish a forecast explicitly marked as a research
candidate, so the experiment produces inspectable output without claiming
qualification.

These rolling retrospective assessment windows can overlap across training
runs. They are candidate qualification measurements, not independent repeated
proof of future profitability. The final block is a **promotion holdout**;
prior research and repeated rolling qualification mean it is not a globally
untouched final test. The runtime separately records forecasts before
their outcomes arrive, then appends matured outcomes and forward metrics.
Forecasts are immutable per symbol, interval, horizon, and decision close: a
later model cannot rewrite an earlier forecast from the same candle.

The five-minute cadence keeps the same qualification comparisons. Under the
fractional policy, block counts grow with usable history and rounding can
leave a boundary unchanged between adjacent builds. Consecutive assessment
blocks still overlap heavily; catch-up and missing usable rows also affect
their boundaries. More frequent candidates do not guarantee more Qualified
models or shorten the reserved-window fitting lag.

Probability metrics do not include trading costs, funding, liquidation risk,
or a tested position-sizing policy. This layer produces research forecasts;
execution and allocation are separate work.

## Expanded offline model research

The research configuration is
[hyperliquid-models-research-70-15-15.json](../configs/hyperliquid-models-research-70-15-15.json).
Run its separate bounded comparison from the repository root:

```powershell
& .\.venv\Scripts\python.exe -m ml.hyperliquid_model_research --config configs/hyperliquid-models-research-70-15-15.json
```

The command accepts the configured native interval and exactly one configured
horizon, currently `5m/h1`. It requires matching exact-time labels and uncapped
70/15/15 splits, records the actual forecast/window durations, and rejects
missing labels or ambiguous multiple horizons before creating research output.

The default experiment evaluates twelve individual models in the currently
installed environment: the four production families; Random Forest,
AdaBoost, Gradient Boosting, LightGBM and XGBoost; and compact CNN, GRU and
CNN+GRU models. CatBoost is optional and was absent in the measured run; its
skip reason is recorded rather than silently substituting another model.
The count depends on available optional dependencies. The compact sequence
implementations have focused tests, but are not production members.

The sequence budget is eight fixed epochs, 32 past timesteps, batch 128,
float32 arrays and bounded CPU threads. Their weights never use calibration
labels for training or early stopping. The shared research preparation fits
one imputer and scaler on fitting rows, uses past-only contiguous windows,
and excludes decisions without complete context across all model families.
Reports distinguish these context exclusions from boundary purges. Calibration
then fits probability calibrators on the later calibration block.

The protocol records models, fixed ensemble weights, settings, copied source
hashes and pinned input hashes before assessment. It compares the current-four
configured weighted blend, the expanded-classical mean, the three-sequence mean, a 50/50 mix of
current-four and sequence-three, and a 75/25 mix of current-four and CNN+GRU.
All prespecified results are reported. A separate control compares the exact
production four-model implementation under fixed-row and fractional splits
on their common final assessment timestamps; this isolates the split change
from the expanded experiment's float32/context preparation. Both controls
retain the same configured calibration strength and estimator weights.

Outputs go only to a newly created directory beneath
`C:/DATASTORE/hyperliquid/_model_research/`, including `protocol.json`, pinned
`inputs/`, copied `source/`, per-market reports and assessment predictions,
`summary.json`, and `REPORT.md`. `--output` must name a new child of that
research namespace. `--skip-sequences` omits the sequence candidates;
`--epochs` and `--sequence-length` declare a different bounded recipe. The
command does not publish `_models` artifacts, alter Paper, or select a winner
for automatic deployment.

See the [expanded 70/15/15 audit](hyperliquid-system-analysis/audits/2026-09-26-expanded-models-70-15-15.md),
[legacy classical audit](hyperliquid-system-analysis/audits/2026-09-26-legacy-classical-model-audit.md),
and [legacy neural audit](hyperliquid-system-analysis/audits/2026-09-26-legacy-neural-model-audit.md)
for measured results, efficiency and reuse decisions. These historical
assessments do not establish statistical significance or profitability after
fees, funding and execution costs.

## Saved files

All live model state lives under the data root's `_models` directory. With the supplied
configuration, a BTC next-five-minute model run is stored under:

```text
C:/DATASTORE/hyperliquid/_models/
  _runtime/
    status.json
    .runtime.lock
    stop.request                 # only while a stop is pending
  BTC/5m/h1/
    runs/<run-id>/
      bundle.joblib
      report.json
      assessment.parquet
    candidate.json
    active.json                  # present after a qualifying publication
    predictions.parquet
    latest_prediction.json
    outcomes.parquet
    forward_metrics.json
```

ETH, HYPE, ZEC, and future symbols follow the same layout. `candidate.json`
points to the latest saved candidate; `active.json` identifies the qualified
bundle selected for inference. Prediction records identify their model and
source-data run, so a forecast can be traced back to its inputs. The data
coordinator's original symbol folders and `_coordinator` controls remain
independent of this model namespace.
