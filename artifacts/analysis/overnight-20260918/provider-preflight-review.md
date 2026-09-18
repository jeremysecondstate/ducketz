# Provider evidence preparation — September 18 Gameplan

Read-only observation: **2026-09-18T04:13:43.913784+00:00**. Native run: `C:/DATASTORE/ml/overnight-runs/20260918T040804.048018Z`; source session September 17; next action date September 18; original deadline September 18 11:00 UTC (04:00 Pacific).

The production watchlist and current native Loop A log agree on eleven symbols: AAPL, AMZN, GOOG, MU, NVDA, SNDK, COST, CROX, PATH, TWST and IONQ. The current base cycle is `20260918T040804.933146Z-pid54404`, status `WRITING`, with all five provider scopes: Databento, FMP, FRED, Schwab and SEC. Observed EQUS.MINI minute/second/hour/day acquisition requests succeed through the exclusive September 18 00:00 UTC boundary. These operational feature requests are separate from the later XNAS.ITCH target-history stage.

The latest inspected stage report is RUNNING at `loop_a_close_fetch`, heartbeat 04:12:34 UTC, with zero reported issues, zero orders and broker orders disabled. At observation, OPRA maintenance had not begun: there are **zero current-run OPRA preflight receipts**, and all **33 existing production cursors** still have exclusive `completed_through=2026-09-17` with no current-run updates. This is pending work, not completed September 17 coverage or evidence of failure. No actionable discrepancy was found.

## Exact OPRA preflight evidence

After the base Loop A fetch, `datafetching.orchestrate` invokes the existing native `datafetching.options_history` maintainer. `options_runtime.py` collects each exact scope's storage/cost preflight sequentially before printing the aggregate guarded-preflight selection summary. Quiet stdout during this phase can be checked using freshly generated local metadata receipts.

Expected receipt pattern:

`C:/DATASTORE/market-data/databento/opra/OPRA.PILLAR/metadata/preflights/{ohlcv-1h,cbbo-1m,definition}/{SYMBOL}.OPT/*_to_2026-09-18/preflight.json`

Require `generated_at` no earlier than the current run's 04:08:04 UTC start, exactly the eleven production symbols times the three schemas, and exact request ranges covering September 17 through exclusive September 18. Verify each semantic checksum by removing `semantic_checksum_sha256` and hashing canonical JSON (`sort_keys=True`, separators `(',', ':')`). Require zero cost both at the receipt total and nested schema estimate, complete cost estimates, passing capacity, zero shortfall and `required_free_bytes = 5 GiB + 2 × estimated_download_size_bytes`. Aggregate selected download estimates must remain within the native 20,000,000,000-byte ceiling. A selection summary or cost estimate is not a completion receipt.

## OPRA completion evidence

Expected native log: 33 distinct `OPRA symbol/schema history:` records with COMPLETE status and the required exclusive end, followed by `Options history maintenance finished:` with 33 completed, zero failed/capacity-blocked/deferred scopes (or fully verified source-bound replay coverage if Historical is delayed).

Small cursor paths:

`C:/DATASTORE/market-data/databento/opra/OPRA.PILLAR/state/symbol-history/{SYMBOL}/{SCHEMA}.json`

Expected native Historical partition receipt paths:

`C:/DATASTORE/market-data/databento/opra/OPRA.PILLAR/{SCHEMA}/{SYMBOL}.OPT/dates/2026-09-17/segments/full-day/{manifest.json,receipt.json}`

Verify the current session's exact dataset/schema/symbol/request bounds, receipt-to-manifest checksum, stored raw/normalized checksum bindings and local sizes, and all 33 cursor exclusive completion dates at least September 18. Preserve the distinction between bounded metadata verification and full native archive validation; do not rescan/re-hash the entire retained archive. `health/current.json` is verified selected inventory, not proof that every retained historical directory is valid.

For the base Loop A completion, require matching `C:/DATASTORE/.ducketz-loop-a-cycle.json` and `.ducketz-loop-a-complete.json` for generation `20260918T040804.933146Z-pid54404`, COMPLETE status, eleven symbols, zero blocking failures, and the eleven per-symbol feature-completion summaries in the current log.

## XNAS target-history evidence

The later `stock_target_history` stage writes its own directory beneath `C:/DATASTORE/ml/stock-target-history-runs/`. Its exact requests are in `manifest.json`; `cost-preflight.json` records request IDs, account-specific estimates, total cost and a strict zero-dollar maximum. `preflight.json` records native size/capacity results. Completion binds these checksums, acquisition counts and zero orders in `receipt.json`. The matching stage log identifies the current native generation. No current-run XNAS acquisition evidence exists yet because this stage has not started. Do not substitute the operational EQUS.MINI evidence for XNAS.ITCH target history.

Reusable bounded examples were read from `C:/dev/ducketz/artifacts/analysis/overnight-20260916/audit_opra_preflight.py` and `audit_provider_completion.py`. Their run, cycle, dates, and historical advisory-count assertions are hard-coded to that earlier night; do not run them unchanged for this attempt. Current evidence must determine the current results.

This review performed only local file/code reads and wrote this note. It made no provider or broker calls, acquired no supervision claim, changed no process or production artifact, and did not revisit accepted historical advisories.

## Follow-up: the two current-cycle optional advisories

Requested follow-up observed **2026-09-18T04:24:53.434218+00:00**, while the same Loop A cycle remained in progress. The current log's AAPL summary (line 164 at observation) records 60 changed Parquets, **zero blocking provider failures, zero optional capture failures**, and two local advisories (`databento=1, fmp=1`). These are shared optional context calculations attributed once in the per-symbol summary.

**CME:** `C:/DATASTORE/pools/cme/CME_CONTEXT/cross-asset-context/databento/diagnostics/CME_CONTEXT_cross-asset-context.parquet` contains one current-cycle diagnostic at **04:19:23.496977 UTC**, severity `advisory`, type `CmeCrossAssetQualityError`. It repeats the known rejection of the **September 3 21:00 UTC** candidate: NQ BBO is stale by **14 days 07:19:24.436428413** against the unchanged **15-minute** maximum. `input_policy=persisted_rows_only` and `provider_rows_preserved=true` remain explicit. The partition-file inventory is unchanged from the prior audit: 18 OHLCV, 290 BBO, 295 MBP files. The native reader at `datafetching/cme_cross_asset_context.py:444` still selects these partitions in preference to flat normalized sources.

All six current CME requests report `status=ok` on their first outer attempt in current log lines 135–146. Fresh normalized metadata confirms the following:

| Scope | OHLCV rows | BBO rows | MBP rows | MBP saturation |
|---|---:|---:|---:|---|
| Context | 2,352 | 300 | 4,995 | True |
| Contracts | 3,718 | 240 | 3,215 | False |

Both MBP receipts retain `limit=5000`, `latest_window_shrink_count=5`, and `empty_window_expansion_count=0`. The context capture remains explicitly capped even though normalization retains 4,995 rows: its actual observations span September 17 **20:13:25.594462719–20:14:00.317715875 UTC**. Contract MBP observations span **20:13:25.594563707–20:14:12.183969323 UTC**. Yesterday both MBP captures were saturated; tonight only the context capture is. Do not describe context MBP as complete book coverage or infer a pass from its normalized row count. OHLCV reaches September 18 03:59 UTC, BBO reaches approximately 04:08 UTC, and no current-cycle CME error metadata was found.

Current normalized evidence paths are under `C:/DATASTORE/pools/cme/CME_CONTEXT/cme_context_{ohlcv-1m,bbo-1m,mbp-10}/databento/normalized/` and the matching `CME_CONTRACTS/cme_contracts_*` directories. Prior comparison: `C:/dev/ducketz/artifacts/analysis/overnight-20260917/cme-advisory-audit.md`.

**FMP:** `C:/DATASTORE/pools/macro/ENERGY_CONTEXT/energy-context/fmp/diagnostics/ENERGY_CONTEXT_energy-context.parquet` still contains exactly one retained September 2 diagnostic and **no current-cycle diagnostic row**. It is the same `advisory` / `FmpEnergyContextQualityError`: provider quote time exceeds receipt by **9.471 seconds**, above the unchanged **5-second** maximum, with persisted-only inputs and original provider rows preserved. The full source at `C:/DATASTORE/pools/macro/CLUSD/quote/fmp/normalized/CLUSD_quote.parquet` now has 1,489 rows and reproduces that precise rejection in the native pure in-memory calculation. The same five historical rows fail the clock-skew rule; the first remains receipt **September 2 13:33:56.529297 UTC** versus market timestamp **13:34:06 UTC** (9.470703 seconds).

The **current-cycle FMP quote passes** the same calculation in memory with one derived row. It was fetched **September 18 04:19:34.904957 UTC**, carries completed-session market time **September 17 20:00 UTC**, and explicitly identifies USO as the CLUSD ETF-share-price proxy. This current receipt does not repair or qualify the retained historical source. Prior comparison: `C:/dev/ducketz/artifacts/analysis/overnight-20260916/provider-advisories.md`.

**Disposition:** no new actionable provider defect or pipeline restart reason established. Preserve both optional derivation rejections, the CME capped-book limitation, source identities, stale/clock-skew thresholds, and raw evidence. This follow-up used only local diagnostic/metadata reads and pure FMP validation in memory, with no provider calls, source writes, pipeline controls or repairs.

## Bounded OPRA preflight snapshot

At **2026-09-18T04:30:02.821826+00:00**, **24/33** fresh exact OPRA scope preflights were available: all eleven `ohlcv-1h`, all eleven `cbbo-1m`, and definitions for AAPL and AMZN. The other nine definitions remained pending. The native sequential preflight phase had not yet emitted its aggregate selection summary. First receipt: 04:27:35.321890 UTC; latest in this snapshot: 04:29:59.306202 UTC.

All 24 observed semantic checksums, exact configured symbol/schema identities, native `databento-opra` / `OPRA.PILLAR` identity, nested request bounds and size sums verify. Each total and nested estimate is exactly **$0** with complete cost estimates. Every capacity result passes the native **5 GiB + 2 × estimated bytes** formula with zero shortfall. The observed aggregate estimate is **4,788,382,608 bytes**, within the 20,000,000,000-byte ceiling; this remains a partial total until all 33 are selected. OHLCV requests cover September 12–18; CBBO and observed definition requests cover September 14–18, all with exclusive September 18 end and therefore including September 17.

All 33 cursors still had exclusive completion September 17 at this snapshot (**0/33** yet prove the required September 17 source-session completion). No acquisition-completion claim is made. No available preflight discrepancy was found; nine definition preflights and native acquisition completion remain the operator's subsequent checks. Machine-readable snapshot: `C:/dev/ducketz/artifacts/analysis/overnight-20260918/opra-preflight-progress.json`. This was one bounded local snapshot with no provider calls or waiting loop.
