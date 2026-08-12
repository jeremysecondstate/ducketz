# Active Pricing

Audited commit: `3fdeca189feffb1d8167f67845503fe7cfb183e1`

Production entrypoint: `python -m ml.option_pricing_runtime` at the 15-minute `+1` phase with exact Loop A readiness required (`docs/datafetch-ml/current_start_command:49-66`; `ml/option_pricing_runtime.py:1470-1709`).

## Python files

- app/models/market_data.py
- datafetching/bar_readiness.py
- datafetching/bar_schema.py
- datafetching/bar_timing.py
- datafetching/decision_time.py
- datafetching/ids.py
- datafetching/layout.py
- datafetching/observability.py
- datafetching/orchestrate.py
- datafetching/parquet_store.py
- datafetching/runtime_lock.py
- ml/artifacts.py
- ml/current_publication.py
- ml/option_pricing/black_scholes.py
- ml/option_pricing/candidate.py
- ml/option_pricing/causal.py
- ml/option_pricing/constraints.py
- ml/option_pricing/consumers.py
- ml/option_pricing/eligibility.py
- ml/option_pricing/lineage.py
- ml/option_pricing/lockbox.py
- ml/option_pricing/loop_native_eligibility.py
- ml/option_pricing/model.py
- ml/option_pricing/operations.py
- ml/option_pricing/opra.py
- ml/option_pricing/opra_materialization.py
- ml/option_pricing/policies.py
- ml/option_pricing/prediction.py
- ml/option_pricing/publication.py
- ml/option_pricing/rates.py
- ml/option_pricing/reporting.py
- ml/option_pricing/schwab_materialization.py
- ml/option_pricing/shadow_model.py
- ml/option_pricing/strategy_outcomes.py
- ml/option_pricing/target_outcome.py
- ml/option_pricing_loop_native_worker.py
- ml/option_pricing_runtime.py
- ml/parquet_contracts.py
- options/features.py
- options/pending_capture.py
- options/publication.py
- options/snapshot.py

## Data providers

This runtime makes no direct external-provider request. It reads receipt-verified Loop A bars/rates, committed Schwab option snapshots produced by Options Capture, and locally persisted OPRA evidence when present. The loop-native worker explicitly records zero external-provider requests (`ml/option_pricing_loop_native_worker.py:29-104`).

## Purpose and functionality

Active Pricing produces target-causal option valuations before Options Capture. For each eligible boundary it waits once for the exact Loop A readiness receipt, constructs live stock/rate inputs, publishes Black-Scholes/BSGP target outcomes, and then performs the broader research/model/evaluation/surface generation under an exclusive owner lock (`ml/option_pricing_runtime.py:223-355`, `ml/option_pricing_runtime.py:1026-1325`, `ml/option_pricing_runtime.py:1547-1709`). The code enforces a separate 1,200-second causal source window (`ml/option_pricing_runtime.py:249-291`, `ml/option_pricing_runtime.py:1124-1131`).

It owns immutable target outcomes plus `ml/option-pricing-target-latest/run.json`, full immutable generations under `ml/option-pricing-runs/<timestamp>/`, and `ml/option-pricing-latest/run.json` (`ml/option_pricing/target_outcome.py:90-192`; `ml/option_pricing_runtime.py:2231-2411`). A child Python worker, launched by this owner, materializes committed Schwab history and trains/reuses the loop-native shadow model locally; it remains part of Active Pricing rather than an independent production runtime (`ml/option_pricing_runtime.py:417-445`; `ml/option_pricing_loop_native_worker.py:29-108`, `ml/option_pricing_loop_native_worker.py:111-190`).

## Inputs from other Loops

- **Producer:** Loop A.
  - **Artifact/data:** Exact bar-readiness receipt and named completed bar files.
  - **Location:** `loop-a/bar-readiness/<target_ns>/` selected by `loop-a/bar-readiness-latest/run.json`.
  - **Use:** Gates target publication and supplies target-causal stock/volatility inputs (`ml/option_pricing_runtime.py:1102-1204`).
- **Producer:** Loop A.
  - **Artifact/data:** Point-in-time `FEDFUNDS` observations.
  - **Location:** FRED current/vintage persisted rate files.
  - **Use:** Supplies the causal risk-free rate used by Black-Scholes and subsequent pricing routes (`ml/option_pricing_runtime.py:307-310`, `ml/option_pricing/rates.py:11-52`).
- **Producer:** Options Capture.
  - **Artifact/data:** Committed Schwab option chain, option-quality features, receipt, and snapshot pointer/history.
  - **Location:** `stocks/<SYMBOL>/options/snapshots/schwab/<snapshot>/` and `stocks/<SYMBOL>/options/latest/schwab.json`, with legacy monthly mirrors under `stocks/<SYMBOL>/options/chains/...` and `features/option-quality/...`.
  - **Use:** Supplies contract state, lagged implied-volatility evidence, evaluation/reconciliation inputs, and the child worker's causal residual samples (`ml/option_pricing_runtime.py:358-445`; `ml/option_pricing/schwab_materialization.py:395-525`; `options/publication.py:51-177`).

Locally persisted OPRA evidence can also feed research/model routes, but it is not produced by one of the six runtime owners and no outbound OPRA request occurs here (`ml/option_pricing_runtime.py:814-835`).

## Outputs for other Loops

- **Artifact/data:** Verified per-target pricing outcome, predictions, manifest, and receipt.
  - **Consumers:** Options Capture and Strategy.
  - **Location:** `ml/option-pricing-target-outcomes/<target-generation>/` selected by `ml/option-pricing-target-latest/run.json`.
  - **Use:** Options waits briefly on this exact target as a coordination barrier; Strategy uses receipt-proven pricing evidence when projecting contracts (`datafetching/options_runtime.py:229-243`; `ml/option_pricing/target_outcome.py:90-192`; `ml/option_pricing/strategy_shadow.py:73-163`).
- **Artifact/data:** Verified compact option-pricing surfaces and full pricing predictions/evaluations.
  - **Consumers:** Directional Loop B and Strategy.
  - **Location:** `ml/option-pricing-runs/<timestamp>/pricing-surfaces.parquet` and companion files, selected atomically by `ml/option-pricing-latest/run.json`.
  - **Use:** Loop B joins compact `opx__` features point-in-time; Strategy loads receipt-verified live/legacy pricing evidence for exact-contract projection (`ml/rolling_materialization.py:648-707`; `ml/option_pricing/consumers.py:58-175`; `ml/option_pricing/strategy_shadow.py:73-163`).
