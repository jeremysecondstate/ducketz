# Loop A

Audited commit: `3fdeca189feffb1d8167f67845503fe7cfb183e1`

Production entrypoint: `python -m datafetching.orchestrate` with Databento, FMP, FRED, Schwab, and SEC; CME and Options are external owners (`docs/datafetch-ml/current_start_command:35-47`; `datafetching/orchestrate.py:37-197`).

## Python files

- app/config.py
- app/models/market_data.py
- app/services/databento_market_data.py
- app/services/databento_retry.py
- app/services/fmp_corporate_data.py
- app/services/fmp_macro_context.py
- app/services/market_fetch_specs.py
- app/services/schwab.py
- app/services/schwab_market_data.py
- app/services/schwab_retry.py
- app/services/schwab_token_store.py
- app/services/sec_capital_structure.py
- datafetching/__init__.py
- datafetching/bar_readiness.py
- datafetching/bar_schema.py
- datafetching/bar_timing.py
- datafetching/calculated_features.py
- datafetching/continuation.py
- datafetching/databento_fetch.py
- datafetching/decision_time.py
- datafetching/derived_bars.py
- datafetching/fmp_energy_context.py
- datafetching/fmp_fetch.py
- datafetching/fred_fetch.py
- datafetching/fred_vintages.py
- datafetching/ids.py
- datafetching/layout.py
- datafetching/loop_a_cycle.py
- datafetching/main.py
- datafetching/observability.py
- datafetching/orchestrate.py
- datafetching/parquet_store.py
- datafetching/quote_liquidity.py
- datafetching/schwab_fetch.py
- datafetching/sec_events.py
- datafetching/sec_fetch.py
- fundamentals/calculation.py
- fundamentals/join.py
- fundamentals/main.py
- fundamentals/parquet_io.py
- fundamentals/point_in_time.py
- ml/artifacts.py
- signals/calculation.py
- signals/consensus.py
- signals/fundamental_context.py
- signals/main.py
- signals/parquet_io.py
- signals/technical_lifecycle.py
- technicals/__init__.py
- technicals/calculations/__init__.py
- technicals/calculations/bar_shape.py
- technicals/calculations/breakout_pressure.py
- technicals/calculations/market_regime.py
- technicals/calculations/session_aware_breakout.py
- technicals/calculations/weekly_context.py
- technicals/main.py
- technicals/parquet_io.py
- technicals/split_adjustments.py

## Data providers

- **Provider:** Databento Historical API
  - **Endpoint:** Python SDK `metadata.get_dataset_range(dataset=<configured equities dataset>)` and `timeseries.get_range(...)` (`app/services/databento_market_data.py:82-102`, `app/services/databento_market_data.py:126-128`, `app/services/databento_market_data.py:149-160`).
  - **Dataset/schema:** Dataset is configured at deployment and is not explicit in the repository. Native schema lanes are `ohlcv-1s`, `ohlcv-1m`, `ohlcv-1h`, and `ohlcv-1d` unless deployment configuration narrows them (`app/services/databento_market_data.py:19-23`, `app/services/market_fetch_specs.py:87-115`).
- **Provider:** Financial Modeling Prep
  - **Endpoint:** Configured base URL, whose repository default is `https://financialmodelingprep.com/stable`, with routes `/profile`, `/quote`, `/market-capitalization-batch` (fallback `/market-capitalization`), `/shares-float`, `/key-metrics`, `/key-metrics-ttm`, `/ratios-ttm`, `/income-statement`, `/balance-sheet-statement`, `/cash-flow-statement`, `/cash-flow-statement-growth`, `/income-statement-growth`, `/financial-growth`, `/splits`, `/economic-indicators`, `/batch-commodity-quotes` (fallback `/quote`), and `/sec-filings-search/symbol` (`app/services/fmp_corporate_data.py:42-88`; `datafetching/fmp_fetch.py:20-42`; `app/services/fmp_macro_context.py:234-260`).
- **Provider:** Federal Reserve Economic Data (FRED)
  - **Endpoint:** `GET https://fred.stlouisfed.org/graph/fredgraph.csv?id=<series_id>` for `GDP`, `CPIAUCSL`, `UNRATE`, and `FEDFUNDS` (`datafetching/fred_fetch.py:15-15`, `datafetching/fred_fetch.py:65-100`, `datafetching/fred_fetch.py:194-196`).
- **Provider:** Schwab Market Data API
  - **Endpoint:** `GET https://api.schwabapi.com/marketdata/v1/quotes` and `GET https://api.schwabapi.com/marketdata/v1/pricehistory` (`app/services/schwab.py:27-27`, `app/services/schwab.py:303-304`, `datafetching/schwab_fetch.py:70-76`). The external Options owner, not Loop A, calls `/chains` under the production configuration (`datafetching/orchestrate.py:167-170`).
- **Provider:** U.S. SEC filing documents
  - **Endpoint:** Not explicit in repository. Loop A discovers filing metadata at FMP `/sec-filings-search/symbol`, selects the filing document URL returned in that metadata, and performs an HTTP GET against that returned URL (`app/services/sec_capital_structure.py:160-194`, `app/services/sec_capital_structure.py:623-643`).

## Purpose and functionality

Loop A is the 15-minute equity and shared-context ingestion/calculation owner. It opens a cycle authority, fetches provider lanes, publishes the exact Databento one-minute bar-readiness receipt as soon as that fast lane finishes, then completes slower provider work and per-symbol fundamental, technical, and consensus-signal calculations before publishing the complete-cycle authority (`datafetching/orchestrate.py:149-197`, `datafetching/orchestrate.py:217-449`).

It owns normalized equity bars and quotes, FMP/FRED/SEC records, quote-liquidity features, fundamental-direction state, technical families, lifecycle signals, the target-specific bar-readiness contract, and `.ducketz-loop-a-cycle.json` / `.ducketz-loop-a-complete.json` (`datafetching/loop_a_cycle.py:15-19`, `datafetching/bar_readiness.py:25-27`). `--cme-mode external` and `--options-mode external` keep CME and chain publication outside this owner (`datafetching/orchestrate.py:167-170`).

## Inputs from other Loops

No cross-loop inputs. Loop A's calculations consume provider data written earlier in the same Loop A cycle; the production flags exclude the CME and Options owners from its write path (`datafetching/orchestrate.py:167-170`, `datafetching/orchestrate.py:234-447`).

## Outputs for other Loops

- **Artifact/data:** Exact Databento one-minute bar-readiness manifest, receipt, and latest pointer.
  - **Consumers:** Active Pricing and Options Capture.
  - **Location:** `loop-a/bar-readiness/<target_ns>/` and `loop-a/bar-readiness-latest/run.json`.
  - **Use:** Both runtimes verify the exact target and source files before causal pricing or committed option publication (`datafetching/bar_readiness.py:82-242`, `datafetching/bar_readiness.py:300-320`; `ml/option_pricing_runtime.py:1102-1131`; `datafetching/options_runtime.py:247-305`).
- **Artifact/data:** Complete Loop A cycle authority.
  - **Consumers:** Directional Loop B and Options Capture.
  - **Location:** `.ducketz-loop-a-complete.json` (with `.ducketz-loop-a-cycle.json` as the active-cycle state).
  - **Use:** Loop B refuses to start its data/model publication without a complete cycle; Options uses the completed time as its regime-evidence cutoff (`datafetching/loop_a_cycle.py:99-167`; `ml/prediction_runtime.py:205-223`; `datafetching/options_runtime.py:312-316`).
- **Artifact/data:** Normalized Databento bars; calculated technicals, fundamentals, and lifecycle signals; Schwab quote-liquidity; FMP energy context; FRED macro/rate state; SEC event features.
  - **Consumer:** Directional Loop B.
  - **Location:** `stocks/<SYMBOL>/bars/...`, `stocks/<SYMBOL>/technicals/...`, `stocks/<SYMBOL>/fundamentals/fundamental-direction/fmp/...`, `stocks/<SYMBOL>/signals/fundamental-technical-lifecycle/consensus/daily.parquet`, `stocks/<SYMBOL>/quotes/features/quote-liquidity/schwab/<YYYY-MM>.parquet`, `pools/macro/features/energy-context/fmp/quote.parquet`, and provider/derived macro and SEC partitions.
  - **Use:** Loop B performs point-in-time joins for the active feature profile (`ml/rolling_materialization.py:415-598`, `ml/rolling_materialization.py:709-761`).
- **Artifact/data:** Completed bars and point-in-time `FEDFUNDS` rate observations.
  - **Consumer:** Active Pricing.
  - **Location:** Loop A bar files named in the readiness receipt and the FRED current/vintage rate authority.
  - **Use:** Pricing constructs target-causal stock inputs, realized-volatility evidence, and risk-free-rate observations (`ml/option_pricing_runtime.py:307-310`, `ml/option_pricing_runtime.py:1105-1219`; `ml/option_pricing/rates.py:11-52`).
- **Artifact/data:** Daily equity bars.
  - **Consumer:** Options Capture.
  - **Location:** `stocks/<SYMBOL>/bars/1d/<provider>/normalized/*.parquet`.
  - **Use:** Options derives realized-volatility evidence before publishing option-quality features (`options/features.py:311-387`; `options/snapshot.py:118-126`).
- **Artifact/data:** Schwab stock BBO quote-liquidity.
  - **Consumer:** Strategy.
  - **Location:** `stocks/<SYMBOL>/quotes/features/quote-liquidity/schwab/<YYYY-MM>.parquet`.
  - **Use:** Strategy joins causal underlying-market bid/ask state to option candidates (`datafetching/quote_liquidity.py:173-197`; `ml/strategy_selection/chain.py:145-174`).
