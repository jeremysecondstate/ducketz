# CME/L2

Audited commit: `3fdeca189feffb1d8167f67845503fe7cfb183e1`

Production entrypoint: `python -m datafetching.cme_runtime` (`docs/datafetch-ml/current_start_command:27-33`; `datafetching/cme_runtime.py:260-408`).

## Python files

- app/models/market_data.py
- app/services/databento_cme_context.py
- app/services/databento_market_data.py
- app/services/databento_retry.py
- app/services/market_fetch_specs.py
- datafetching/bar_schema.py
- datafetching/bar_timing.py
- datafetching/calculated_features.py
- datafetching/cme_cross_asset_context.py
- datafetching/cme_history.py
- datafetching/cme_runtime.py
- datafetching/ids.py
- datafetching/layout.py
- datafetching/observability.py
- datafetching/parquet_store.py
- datafetching/runtime_lock.py
- ml/artifacts.py

## Data providers

- **Provider:** Databento Historical API
  - **Endpoint:** Python SDK `metadata.get_dataset_range(dataset=<configured CME dataset>)` and `timeseries.get_range(dataset=<configured CME dataset>, schema=<configured schema>, symbols=..., stype_in=..., start=..., end=...)` (`app/services/databento_cme_context.py:247-247`, `app/services/databento_cme_context.py:285-291`, `app/services/databento_cme_context.py:329-329`).
  - **Dataset/schema:** Dataset is configured at deployment and is not explicit in the repository. The production owner schedules configured schemas; built-in cadence policies cover `ohlcv-1m`, `bbo-1m`, and `mbp-10` (`datafetching/cme_runtime.py:37-60`).

## Purpose and functionality

This owner continuously fetches CME futures context and order-book records under one exclusive runtime lock. Each configured schema has its own cadence, phase, overlap, chunk, and record-limit policy. A cycle resumes from a persisted cursor, fetches bounded Databento ranges, appends immutable event-history partitions, advances the cursor, derives hourly cross-asset context, and publishes the current L2 snapshot/pointer (`datafetching/cme_runtime.py:98-225`, `datafetching/cme_runtime.py:355-408`, `datafetching/cme_runtime.py:474-543`).

It owns the CME event histories and cursors, the derived cross-asset feature file, and the current L2 snapshot authority. The derived feature is stored at `pools/cme/features/cross-asset-context/databento/1h.parquet` (`datafetching/cme_cross_asset_context.py:277-285`).

## Inputs from other Loops

No cross-loop inputs. This owner ingests Databento directly and resumes from its own CME cursors/history (`datafetching/cme_runtime.py:118-150`, `datafetching/cme_runtime.py:508-543`).

## Outputs for other Loops

- **Artifact/data:** Hourly CME cross-asset context.
  - **Consumer:** Directional Loop B.
  - **Location:** `pools/cme/features/cross-asset-context/databento/1h.parquet`.
  - **Use:** Loop B joins the verified point-in-time CME feature family into each horizon's model matrix; only when the derived file is absent does it derive context from normalized CME source files (`ml/rolling_materialization.py:763-806`).

The owner also publishes event histories/cursors and a current L2 snapshot/pointer, but no other one of the six production runtime owners reads that L2 pointer in this commit (`datafetching/cme_runtime.py:192-215`; `datafetching/cme_history.py:207-288`).

