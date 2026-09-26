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

## First measured run

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
  "horizons_bars": [4],
  "retrain_seconds": 900,
  "poll_seconds": 5,
  "retry_seconds": 60,
  "model_threads": 2,
  "min_train_rows": 1000,
  "calibration_rows": 192,
  "assessment_rows": 288,
  "max_model_age_seconds": 86400
}
```

`markets_config` resolves relative to the model configuration's directory.
That existing market configuration supplies the symbol list, candle interval,
and data root. Add or remove symbols there; do not duplicate the universe in
the model config or copy Python folders. The model runtime rereads market
membership while running. A removed market's training may finish, but the
runtime does not publish that result after the removal has been applied.

The initial horizon is four candles: one hour for the current 15-minute
dataset. Other configured horizons must have corresponding future-return
columns in the input labels. The data pipeline initially provides horizons
of 1, 4, and 16 candles. A one-hour horizon means a new one-hour-ahead forecast
on each completed 15-minute candle; it does not mean four precommitted trades.

The current retraining cadence is **every 15 minutes** (`retrain_seconds=900`),
changed from hourly on 2026-09-26. A fresh one-hour-ahead prediction is still
generated for each new 15-minute candle. Retraining follows elapsed candle
history since the candidate's input snapshot and requires a changed source
run, so fitting duration does not gradually move the schedule later. This is
an explicit configuration override; the loader's fallback default remains
3,600 seconds when the setting is omitted.
Changing training settings requires a runtime restart; symbol membership
continues to come from the shared market config.

### Optional fitting-window cap (not enabled)

`max_train_rows` is an available configuration field, but it was **not enabled**
after the September 26 offline comparison. Production remains uncapped with
192 calibration rows and 288 assessment rows, as shown above. Omitting the field
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

The runtime polls for completed data snapshots every five seconds. Predictions
use an already saved model. New model fits run in a background worker thread,
with at most one training job globally and two numerical threads by
default. Training does not run inside the data-fetching process or block the
runtime's inference polling. There is no need to refit on every polling tick.

The initial fixed ensemble contains logistic regression, Extra Trees,
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
proof of future profitability. The runtime separately records forecasts before
their outcomes arrive, then appends matured outcomes and forward metrics.
Forecasts are immutable per symbol, interval, horizon, and decision close: a
later model cannot rewrite an earlier forecast from the same candle.

The 15-minute cadence keeps the same qualification comparisons and partition
sizes. With regular complete input, consecutive builds shift the 288-row
assessment by one 15-minute row, sharing 287 rows; catch-up or missing usable
rows can change that step. More frequent candidates do not guarantee more
Qualified models. They also do not shorten the reserved-window fitting lag
(123 hours in the first measured run above); exact cutoffs remain in each
report.

Probability metrics do not include trading costs, funding, liquidation risk,
or a tested position-sizing policy. This layer produces research forecasts;
execution and allocation are separate work.

## Saved files

All new state lives under the data root's `_models` directory. With the supplied
configuration, a BTC one-hour model run is stored under:

```text
C:/DATASTORE/hyperliquid/_models/
  _runtime/
    status.json
    .runtime.lock
    stop.request                 # only while a stop is pending
  BTC/15m/h4/
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
