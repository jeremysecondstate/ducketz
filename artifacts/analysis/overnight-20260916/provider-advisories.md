# Current Loop A optional provider advisories

Read-only check completed **2026-09-16 04:26:11 UTC** (September 15 21:26:11 Pacific). Native cycle `20260916T040758.032086Z-pid29216` was still `WRITING`; this is not a final provider-completion report.

The AAPL summary's `databento=1, fmp=1` entries account for two shared optional context derivations. It explicitly reports **zero blocking provider failures and zero optional capture failures**. The CME case is covered in the [current CME audit](C:/dev/ducketz/artifacts/analysis/overnight-20260916/cme-advisory-audit.md).

The FMP case is the same retained **`advisory` / `FmpEnergyContextQualityError`** seen in the prior-day audit: “FMP provider quote timestamp exceeds local receipt by **9.471s** (maximum allowed clock skew is **5.000s**)”. Its identity/message is deduplicated, so the diagnostic retains its original September 2 timestamp rather than creating a current-cycle row. `input_policy=persisted_rows_only` and `provider_rows_preserved=true` remain explicit.

The complete CLUSD/USO quote source contains 1,487 rows and reproduces that exact rejection in the unchanged native pure calculation. There are still five historical rows exceeding the five-second threshold, and the first offending row is unchanged: receipt September 2 13:33:56.529297 UTC versus provider timestamp 13:34:06 UTC (9.470703 seconds).

The **current-cycle quote passes** the same native calculation in memory (one derived row). Its source receipt is September 16 **04:21:31.419659 UTC**, provider market timestamp September 15 **20:00 UTC**, with explicit USO ETF proxy identity. This is fresh acquisition of the completed-session quote; it does not repair or replace the retained historical rows or make the full optional history qualified.

No new actionable FMP source defect was identified. Preserve the existing advisory and clock-skew threshold. No provider retry, source mutation or overnight repair is justified solely by this repeated limitation. No broker/provider calls, claims, process actions, production edits or tests were made.

Evidence: [current JSON and comparison](C:/dev/ducketz/artifacts/analysis/overnight-20260916/provider-advisories.json), [native Loop A log](C:/DATASTORE/ml/overnight-runs/20260916T040757.217937Z/loop_a_close_fetch.log), [FMP diagnostic](C:/DATASTORE/pools/macro/ENERGY_CONTEXT/energy-context/fmp/diagnostics/ENERGY_CONTEXT_energy-context.parquet), [quote source](C:/DATASTORE/pools/macro/CLUSD/quote/fmp/normalized/CLUSD_quote.parquet), [prior-day audit](C:/dev/ducketz/artifacts/analysis/overnight-20260915/loop-a-provider-advisory-audit.md).
