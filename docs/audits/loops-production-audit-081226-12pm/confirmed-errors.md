# Confirmed Errors

### Empty pricing materialization breaks Strategy offline replay

- **Runtime:** Strategy
- **Failure:** The current empty loop-native pricing materialization is decoded into a zero-column frame and Strategy unconditionally indexes `sample_status`, producing `KeyError: 'sample_status'`. The exception is caught at the catalog boundary, so the Strategy run publishes without the offline-replay pricing lane and records the load error.
- **Evidence:** `ml/option_pricing/schwab_materialization.py:1751-1764`; `ml/option_pricing/strategy_shadow.py:110-120`; `ml/option_pricing/strategy_shadow.py:466-473`
- **Production path:** `python -m ml.strategy_runtime` -> `run_strategy_once` -> `run_strategy_selection(..., pricing_mode="active")` -> `load_strategy_pricing_evidence(..., include_offline_replay=True)` -> `_offline_replay_predictions` -> `_cached_offline_replay_predictions` (`docs/datafetch-ml/current_start_command:116-122`; `ml/strategy_runtime.py:63-139`; `ml/strategy_selection/runtime.py:90-103`; `ml/option_pricing/strategy_shadow.py:380-473`).
- **Proof:** The producer deliberately writes an `id`-only Parquet for zero samples (`ml/option_pricing/schwab_materialization.py:1751-1764`). The currently selected immutable materialization `ml/option-pricing-loop-native-materializations/20260811T182829.497679Z/causal-residual-samples.parquet` has zero rows and the sole column `id`. Re-running `load_strategy_pricing_evidence` against that authority returns `("offline_replay:KeyError:'sample_status'",)`. The immutable production artifact `ml/strategy-runs/20260812T184000.082911Z/strategy-model-reports.json` independently records the same error.
- **Impact:** Offline-replay pricing predictions from the selected materialization are omitted from the published Strategy pricing-evidence catalog for every active-pricing Strategy cycle that selects this current materialization.

