# Options Capture

Audited commit: `3fdeca189feffb1d8167f67845503fe7cfb183e1`

Production entrypoint: `python -m datafetching.options_runtime` at the 15-minute `+6` phase (`docs/datafetch-ml/current_start_command:68-88`; `datafetching/options_runtime.py:500-596`).

## Python files

- app/config.py
- app/models/market_data.py
- app/services/market_fetch_specs.py
- app/services/schwab.py
- app/services/schwab_market_data.py
- app/services/schwab_retry.py
- app/services/schwab_token_store.py
- datafetching/bar_readiness.py
- datafetching/bar_schema.py
- datafetching/bar_timing.py
- datafetching/decision_time.py
- datafetching/ids.py
- datafetching/layout.py
- datafetching/loop_a_cycle.py
- datafetching/observability.py
- datafetching/options_runtime.py
- datafetching/orchestrate.py
- datafetching/parquet_store.py
- datafetching/pricing_barrier.py
- datafetching/runtime_lock.py
- datafetching/schwab_fetch.py
- ml/artifacts.py
- ml/option_pricing/policies.py
- ml/option_pricing/target_outcome.py
- ml/parquet_contracts.py
- options/__init__.py
- options/features.py
- options/pending_capture.py
- options/publication.py
- options/snapshot.py

## Data providers

- **Provider:** Schwab Market Data API
  - **Endpoint:** `GET https://api.schwabapi.com/marketdata/v1/chains` (`app/services/schwab.py:26-29`; `datafetching/schwab_fetch.py:79-114`).

## Purpose and functionality

This owner captures one Schwab option chain per configured symbol for each eligible market target. It first reconciles prior pending captures, determines the current target/discovery clock, waits briefly for the matching Pricing target outcome, and verifies exact Loop A bar readiness. With readiness it publishes an immutable committed snapshot; without readiness it checksum-seals the raw provider response under the separate pending authority and later reconciles it only if readiness arrives inside the causal window (`datafetching/options_runtime.py:80-99`, `datafetching/options_runtime.py:229-442`, `datafetching/options_runtime.py:637-668`).

It owns `options/pending-captures/schwab/<target>/<symbol>/`, immutable snapshot generations under `stocks/<SYMBOL>/options/snapshots/schwab/`, per-symbol latest pointers, and the monthly raw/normalized chain and option-quality mirrors (`options/pending_capture.py:100-118`; `options/publication.py:51-177`; `options/snapshot.py:128-133`).

## Inputs from other Loops

- **Producer:** Active Pricing.
  - **Artifact/data:** Exact target pricing outcome/barrier.
  - **Location:** `ml/option-pricing-target-outcomes/<target-generation>/` selected by `ml/option-pricing-target-latest/run.json`.
  - **Use:** The runtime waits up to the configured 45 seconds and embeds the verified barrier evidence when available; absence does not suppress the Schwab capture (`datafetching/options_runtime.py:229-243`).
- **Producer:** Loop A.
  - **Artifact/data:** Exact bar-readiness receipt and named completed one-minute bars.
  - **Location:** `loop-a/bar-readiness/<target_ns>/` selected by `loop-a/bar-readiness-latest/run.json`.
  - **Use:** Determines whether a response can be committed now or must remain in the pending authority (`datafetching/options_runtime.py:247-305`, `datafetching/options_runtime.py:320-409`).
- **Producer:** Loop A.
  - **Artifact/data:** Complete-cycle `finished_at` and completed daily bars.
  - **Location:** `.ducketz-loop-a-complete.json` and `stocks/<SYMBOL>/bars/1d/<provider>/normalized/*.parquet`.
  - **Use:** Sets the point-in-time regime cutoff and supplies realized-volatility evidence for option-quality features (`datafetching/options_runtime.py:312-316`; `options/features.py:311-387`; `options/snapshot.py:118-126`).

## Outputs for other Loops

- **Artifact/data:** Immutable committed Schwab raw chain, normalized contracts, option-quality features, manifest, receipt, and latest pointer.
  - **Consumers:** Active Pricing, Directional Loop B, and Strategy.
  - **Location:** `stocks/<SYMBOL>/options/snapshots/schwab/<snapshot>/` and `stocks/<SYMBOL>/options/latest/schwab.json`; monthly compatibility mirrors are under `stocks/<SYMBOL>/options/chains/schwab/{raw,normalized}/<YYYY-MM>.parquet` and `stocks/<SYMBOL>/options/features/option-quality/schwab/<YYYY-MM>.parquet`.
  - **Use:** Pricing builds lagged-IV/model samples; Loop B joins point-in-time option-quality features; Strategy constructs and prices exact contracts and derives observed entry/exit outcomes (`options/publication.py:73-177`, `options/snapshot.py:128-133`; `ml/rolling_materialization.py:600-647`; `ml/strategy_selection/runtime.py:109-220`).

Pending captures are consumed and promoted only by this same owner; they are not cross-loop production outputs (`options/pending_capture.py:851-858`; `datafetching/options_runtime.py:91-99`).

