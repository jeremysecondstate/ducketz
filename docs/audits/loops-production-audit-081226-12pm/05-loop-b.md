# Directional Loop B

Audited commit: `3fdeca189feffb1d8167f67845503fe7cfb183e1`

Production entrypoint: `python -m ml.prediction_runtime` at the 15-minute `+5` phase, using the `databento` persisted namespace, active v2 feature profile, logistic models, and Platt calibration (`docs/datafetch-ml/current_start_command:90-110`; `ml/prediction_runtime.py:26-223`).

## Python files

- app/models/market_data.py
- datafetching/bar_readiness.py
- datafetching/bar_schema.py
- datafetching/bar_timing.py
- datafetching/calculated_features.py
- datafetching/decision_time.py
- datafetching/fmp_energy_context.py
- datafetching/ids.py
- datafetching/layout.py
- datafetching/loop_a_cycle.py
- datafetching/observability.py
- datafetching/parquet_store.py
- datafetching/pricing_barrier.py
- datafetching/quote_liquidity.py
- datafetching/runtime_lock.py
- fundamentals/join.py
- fundamentals/parquet_io.py
- ml/artifacts.py
- ml/calendars.py
- ml/calibration.py
- ml/contracts.py
- ml/current_publication.py
- ml/datasets/families.py
- ml/datasets/point_in_time.py
- ml/datasets/technical.py
- ml/feature_registry.py
- ml/horizons.py
- ml/live_evidence.py
- ml/model_features.py
- ml/model_runtime.py
- ml/models/registry.py
- ml/option_pricing/consumers.py
- ml/option_pricing/publication.py
- ml/parquet_contracts.py
- ml/prediction_runtime.py
- ml/preprocessing.py
- ml/rolling_materialization.py
- ml/rolling_samples.py
- ml/runtime_pipeline.py
- ml/strategy_selection/contracts.py
- ml/strategy_selection/registry.py
- ml/strategy_selection/research_trace.py
- ml/timing.py
- ml/universe.py
- options/__init__.py
- options/features.py
- options/publication.py
- options/snapshot.py
- technicals/calculations/bar_shape.py
- technicals/calculations/breakout_pressure.py
- technicals/calculations/market_regime.py
- technicals/calculations/session_aware_breakout.py
- technicals/calculations/weekly_context.py
- technicals/parquet_io.py
- technicals/split_adjustments.py

## Data providers

This runtime makes no direct external-provider request. Its `--provider databento` argument selects persisted Loop A bar paths; it does not cause an outbound Databento call (`ml/prediction_runtime.py:26-168`; `ml/rolling_samples.py:367-438`).

## Purpose and functionality

Directional Loop B waits for a complete Loop A cycle, materializes point-in-time rolling samples for `1h`, `4h`, `1d`, and the expanded weekly horizons, fits/reuses per-horizon directional classifiers, applies calibration, creates live and evaluated predictions, and publishes samples, predictions, evaluations, monitoring, and intelligence as one immutable generation (`ml/prediction_runtime.py:185-223`; `ml/runtime_pipeline.py:250-674`, `ml/runtime_pipeline.py:715-821`).

It owns immutable runs under `ml/runs/<timestamp>/`, the atomic `ml/latest/run.json` authority, and the legacy current intelligence mirror at `ml-intelligence/latest/rolling-predictions.parquet` (`ml/runtime_pipeline.py:788-821`, `ml/runtime_pipeline.py:847-1019`).

## Inputs from other Loops

- **Producer:** Loop A.
  - **Artifact/data:** Complete-cycle authority.
  - **Location:** `.ducketz-loop-a-complete.json`.
  - **Use:** Required before materialization/training can begin (`ml/prediction_runtime.py:205-223`).
- **Producer:** Loop A.
  - **Artifact/data:** Normalized Databento bars, technical features, FMP fundamentals, lifecycle signals, Schwab quote-liquidity, FMP energy context, FRED macro context, and SEC event features.
  - **Location:** The Loop A stock/pool paths enumerated in `02-loop-a.md`.
  - **Use:** Joined causally by feature family and horizon into the model matrix (`ml/rolling_materialization.py:415-598`, `ml/rolling_materialization.py:709-761`).
- **Producer:** CME/L2.
  - **Artifact/data:** Hourly cross-asset CME context.
  - **Location:** `pools/cme/features/cross-asset-context/databento/1h.parquet`.
  - **Use:** Joined as the active profile's `cme__` feature family; normalized CME sources are only a fallback when the derived file is absent (`ml/rolling_materialization.py:763-806`).
- **Producer:** Options Capture.
  - **Artifact/data:** Receipt-verified committed Schwab option-quality snapshots.
  - **Location:** `stocks/<SYMBOL>/options/snapshots/schwab/<snapshot>/`, selected through committed receipts/pointers.
  - **Use:** Joined as the `opt__` feature family at each causal cutoff (`ml/rolling_materialization.py:600-647`).
- **Producer:** Active Pricing.
  - **Artifact/data:** Receipt-verified compact pricing surfaces.
  - **Location:** `ml/option-pricing-runs/<timestamp>/pricing-surfaces.parquet`, selected by `ml/option-pricing-latest/run.json`.
  - **Use:** Joined as `opx__` features with target-time, first-availability, freshness, schema, and automation checks (`ml/rolling_materialization.py:648-707`; `ml/option_pricing/consumers.py:58-175`).

## Outputs for other Loops

- **Artifact/data:** Current immutable Loop B samples, predictions, evaluations, monitoring, intelligence, manifest, and publication receipt.
  - **Consumer:** Strategy.
  - **Location:** `ml/runs/<timestamp>/` selected by `ml/latest/run.json`.
  - **Use:** Strategy reads the exact published `samples.parquet` and `predictions.parquet`, limits the universe from the manifest configuration, and preserves the Loop B receipt in its source lineage (`ml/strategy_runtime.py:63-139`).
