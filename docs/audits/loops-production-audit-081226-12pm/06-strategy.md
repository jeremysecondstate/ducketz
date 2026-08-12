# Strategy

Audited commit: `3fdeca189feffb1d8167f67845503fe7cfb183e1`

Production entrypoint: `python -m ml.strategy_runtime` at the 15-minute `+10` phase with active pricing (`docs/datafetch-ml/current_start_command:112-122`; `ml/strategy_runtime.py:316-448`).

## Python files

- app/models/market_data.py
- datafetching/bar_readiness.py
- datafetching/bar_schema.py
- datafetching/bar_timing.py
- datafetching/decision_time.py
- datafetching/ids.py
- datafetching/layout.py
- datafetching/observability.py
- datafetching/parquet_store.py
- datafetching/pricing_barrier.py
- datafetching/quote_liquidity.py
- datafetching/runtime_lock.py
- ml/artifacts.py
- ml/calibration.py
- ml/current_publication.py
- ml/option_pricing/black_scholes.py
- ml/option_pricing/causal.py
- ml/option_pricing/constraints.py
- ml/option_pricing/model.py
- ml/option_pricing/policies.py
- ml/option_pricing/prediction.py
- ml/option_pricing/publication.py
- ml/option_pricing/schwab_materialization.py
- ml/option_pricing/shadow_model.py
- ml/option_pricing/strategy_shadow.py
- ml/option_pricing/target_outcome.py
- ml/parquet_contracts.py
- ml/preprocessing.py
- ml/strategy_publication.py
- ml/strategy_runtime.py
- ml/strategy_selection/__init__.py
- ml/strategy_selection/candidates.py
- ml/strategy_selection/chain.py
- ml/strategy_selection/contracts.py
- ml/strategy_selection/market_state.py
- ml/strategy_selection/model.py
- ml/strategy_selection/registry.py
- ml/strategy_selection/research_trace.py
- ml/strategy_selection/runtime.py
- options/__init__.py
- options/features.py
- options/publication.py
- options/snapshot.py

## Data providers

This runtime makes no direct external-provider request. `provider="schwab"` labels its persisted chain/BBO evidence; all chain and stock-quote inputs were captured earlier by Options Capture and Loop A (`ml/strategy_runtime.py:100-139`; `ml/strategy_selection/chain.py:145-174`).

## Purpose and functionality

Strategy consumes exactly one published Loop B generation, builds historical option-strategy outcomes from committed Schwab snapshots, loads receipt-verified Active Pricing evidence, fits/reuses profitable-outcome classifiers when the evidence contract is satisfied, scores and ranks live exact-contract candidates, and publishes an independent Strategy generation (`ml/strategy_runtime.py:63-267`; `ml/strategy_selection/runtime.py:68-347`).

It owns immutable `ml/strategy-runs/<timestamp>/` generations containing `strategy-candidates.parquet`, `strategy-audit.parquet`, `strategy-model-reports.json`, `manifest.json`, and `publication.json`, selected by `ml/strategy-latest/run.json` (`ml/strategy_runtime.py:146-267`; `ml/strategy_publication.py:37-107`).

## Inputs from other Loops

- **Producer:** Directional Loop B.
  - **Artifact/data:** Current samples, directional predictions, manifest, and publication receipt.
  - **Location:** `ml/runs/<timestamp>/` selected by `ml/latest/run.json`.
  - **Use:** Defines the causal sample history, live directional probability, symbol universe, horizons, and source lineage for candidate construction (`ml/strategy_runtime.py:63-139`).
- **Producer:** Options Capture.
  - **Artifact/data:** Immutable committed Schwab normalized chains, option-quality features, and receipts across entry and exit timestamps.
  - **Location:** `stocks/<SYMBOL>/options/snapshots/schwab/<snapshot>/` and `stocks/<SYMBOL>/options/latest/schwab.json`.
  - **Use:** Builds exact option spreads, observed BBO entry/exit outcomes, and the live chain state (`ml/strategy_selection/runtime.py:109-347`; `ml/strategy_selection/chain.py:183-371`).
- **Producer:** Active Pricing.
  - **Artifact/data:** Receipt-verified target/main pricing predictions and locally materialized replay evidence.
  - **Location:** `ml/option-pricing-target-outcomes/...`, `ml/option-pricing-runs/...`, and `ml/option-pricing-loop-native-materializations/...`, selected by their verified pointers/receipts.
  - **Use:** Attaches exact-contract model/fallback valuations before model fitting and live scoring (`ml/strategy_selection/runtime.py:90-103`; `ml/option_pricing/strategy_shadow.py:73-163`, `ml/option_pricing/strategy_shadow.py:380-449`).
- **Producer:** Loop A.
  - **Artifact/data:** Schwab stock BBO quote-liquidity.
  - **Location:** `stocks/<SYMBOL>/quotes/features/quote-liquidity/schwab/<YYYY-MM>.parquet`.
  - **Use:** Adds causal underlying-market state to chain-derived candidates (`ml/strategy_selection/chain.py:145-174`).

## Outputs for other Loops

No verified cross-loop outputs. Strategy publishes its own current candidate/audit authority, but none of the other five production runtime owners consumes `ml/strategy-latest/run.json` in the authoritative path in this commit.

