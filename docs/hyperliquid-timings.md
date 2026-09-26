# Hyperliquid operation timing

Data and model workers retain timing measurements independently of their
scheduling intervals. A 15-minute candle interval is not 15 minutes of work;
the five-second model polling interval is not five seconds of fitting.

From `C:/dev/ducketz`:

```powershell
& .\.venv\Scripts\python.exe -m ml.hyperliquid_timings --export
```

This reads saved artifacts and journals; it does not fetch data, fit models,
publish candidates or trade. The default window is the last 24 hours. Use
`--hours 168` for seven days or `--output-root PATH` for another datastore.

Exports under `C:/DATASTORE/hyperliquid/_timings/`:

- `report.md`: readable latest/median/p95/maximum timings, sample counts, and
  complete observed multi-symbol batch spans.
- `summary.json`: the same aggregates plus every available stage, including
  queue, worker, publication, and end-to-end measurements.
- `timings.parquet`: one row per measured attempt or historical published run,
  with symbol, timeframe/horizon, source/model IDs, stage durations and outcome.

These exports are point-in-time views. Run the command again to refresh them.
The underlying workers keep recording their journals without running a separate
monitor or adding a new prerequisite for data or model publication.

## Data boundaries

Each symbol's existing `15m/loop_events.jsonl` retains `last_cycle_timing` for
successful updates, unchanged checks, and failures. Current loop/coordinator
status also exposes the latest measurement. Durations use a monotonic timer.

| Field | Included work |
| --- | --- |
| `fetch_and_normalize` | API request and normalization of returned candles |
| `feature_build` | Shared feature calculations across the stored history |
| `parquet_and_catalog_write` | OHLCV/features/labels Parquets and feature catalog writes |
| `total_before_publish` | Pipeline work before final summary and latest-pointer publication; includes loading, merge, labels and validation overhead |
| `last_cycle_timing.total_seconds` | Complete outer attempt including the coordinator queue, pipeline and publication; excludes the timing journal write |
| `queue_wait_seconds` | Time waiting for one of the coordinator's update slots, when measured |

Onboarding, routine one-candle updates, multi-candle catch-up, reconciliation,
benchmarking, unchanged checks, and failures are reported separately. An
unchanged check retains its own fetch duration and zero feature/write work;
it does not reuse timings from the snapshot it inspected. Historical logs
without a measured duration remain missing rather than becoming zero.

## Model boundaries

Each immutable model `report.json` already retains per-estimator fit,
calibration and assessment-prediction timings. `total_seconds` covers the
complete candidate build, including preparation, final metrics and eligibility
comparisons. Assessment-prediction time alone is not the entire evaluation.

`_models/_runtime/training_events.jsonl` now retains one event per collected
job. `outcome` distinguishes promotion, research-only publication, training or
publication failures, and discarded jobs. `event: training_completed` means
the attempt was collected; inspect `outcome` to determine success.

| `timing` field | Meaning |
| --- | --- |
| `worker_seconds` | Actual background candidate-building work |
| `queue_wait_seconds` | Submission to worker start |
| `collection_wait_seconds` | Worker finish to the runtime observing completion |
| `publication_seconds` | Saving the bundle, report and assessment, and updating candidate/active pointers |
| `operation_seconds` | Worker plus publication duration |
| `end_to_end_seconds` | Submission through publication/collection completion, including waits |

The legacy `last_training_seconds`/`completed_jobs.seconds` fields still mean
submission-to-collection time. They can be approximately five seconds when
the actual candidate takes about 1.3 seconds, because the continuous runtime
polls every five seconds. Prefer `last_training_timing` for the breakdown.
The new journal survives worker restarts; a journal write failure is reported
in status and does not turn a published candidate into a failed fit.

## Interpreting the comparison

Before adding the complete-cycle instrumentation, recent live snapshots on
September 25, 2026 showed about **0.5 seconds per symbol** for data pipeline
work before final publication, including about **0.07 seconds** for 38 feature
columns across approximately 5,000 rows. The latest four-symbol data cycle's
stored start/finish timestamps spanned about **1.04 seconds** with two update
slots. Recent candidate reports showed **1.34–1.35 seconds per symbol** for
the four-model ensemble's preparation, fit, calibration and evaluation.

These are recorded measurements of the current recipe. The earlier BERA
system's roughly 25-minute data and model stages used a different calculation
and model lineup and potentially a different history size. Faster runtime
does not establish equal predictive quality. The small neural network has a
bounded iteration budget; its convergence warnings remain in model reports.

The report does not sum concurrent symbol durations and call that elapsed
time. It reports a complete cohort span only when every configured symbol has
start/end evidence for the same source candle close. Such cross-symbol spans
use UTC clocks; individual durations use monotonic clocks. Data and model
stages run independently, so their durations should not be summed as though
the system were one synchronous 50-minute loop.

The p95 is an empirical percentile over the displayed sample window. Always
read its sample count, especially while there are only a few hourly model fits.
This change measures the existing schedules and policies; it does not alter
their frequencies or add real-money execution.
