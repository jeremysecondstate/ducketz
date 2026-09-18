# Current provider and OPRA evidence audit

Initial verification observed **2026-09-18T04:41:40.513080+00:00** for overnight run `C:/DATASTORE/ml/overnight-runs/20260918T040804.048018Z`, preparing September 18 from completed source session September 17. **All audited current provider/preflight/cursor/partition checks pass; no discrepancies found.** Completion reconciliation at **05:21:09.102254 UTC** verifies the native OPRA maintenance result and **Loop A COMPLETE, exit code 0, at 05:19:26.916919 UTC**. The pipeline has advanced to Directional generation; this audit does not claim whole-overnight completion.

The matching base Loop A cycle and completion receipts identify `20260918T040804.933146Z-pid54404`, all eleven current production symbols, all five configured providers (Databento, FMP, FRED, Schwab and SEC), COMPLETE status and zero failures. Base calculation completion time is **04:27:28.741812 UTC**.

## Production OPRA

- **33/33 fresh exact symbol/schema preflights** verify, covering eleven symbols × `ohlcv-1h`, `cbbo-1m` and `definition`. Every semantic checksum, native provider/dataset identity, nested scope/range and size sum matches. Every total and nested estimate is exactly **$0** with complete cost estimates.
- The native guarded-preflight log selects all 33, defers none, and agrees with the audited total **4,837,988,448 estimated download bytes**, below the 20,000,000,000-byte ceiling. All capacity checks pass with zero shortfall and the native **5 GiB + 2 × estimated bytes** formula. Minimum observed free space: **1,345,451,024,384 bytes**; largest single-scope requirement: **7,971,467,520 bytes**.
- **33/33 current production cursors** now have exclusive `completed_through=2026-09-18`, proving native completion through September 17. Each cursor has the correct symbol, parent symbol, schema, provider and dataset, a current-run update time, and no Live replay substitution.
- **33/33 September 17 Historical partitions** have exact full-day request bounds **September 17 00:00–September 18 00:00 UTC**, correct OPRA.PILLAR/schema/parent identities and `timeseries-stream` delivery. Every receipt-to-manifest checksum and publication time verifies.
- Recomputed SHA-256 for the **66 raw/normalized files belonging only to those 33 current-session partitions**, totaling **555,540,055 bytes**. All actual hashes equal both manifest and receipt bindings, and all sizes match. These partitions contain **15,030,808 normalized rows**. No retained-history or whole-archive scan was performed.
- The current log contains exactly **33 distinct COMPLETE scope lines**, all ending September 18. The final native maintenance summary confirms **33 requested, 33 preflighted, 33 completed**, with **zero capacity-blocked, failed, bootstrap-required or deferred scopes**, zero Live replay scopes/bytes, the same 4,837,988,448-byte estimate and **$0** selected estimated cost. Native maintenance exits 0 for September 18.

The rebuilt `C:/DATASTORE/market-data/databento/opra/OPRA.PILLAR/health/current.json` is observed at **2026-09-18T05:19:12.304991+00:00**, before Loop A completion. Its selected verified inventory contains **66,383 partitions**, **4,857,224,934 rows**, 64,027,498,335 normalized bytes and 81,191,086,475 raw bytes. Production schema inventory counts are `ohlcv-1h`: **14,564 partitions / 159,525,511 rows**; `cbbo-1m`: **1,278 / 766,844,290**; `definition`: **7,233 / 6,659,814**. Schema count/row sums match the saved inventory totals.

This health inventory reports native selected verified partitions. It does not enumerate skipped or invalid retained directories, so it does **not** establish that every retained archive directory is valid. Completion reconciliation read only terminal log summaries, the stage report and this small health JSON; no retained-archive rescan or repeated data-file hashing was performed.

## Provider captures and calculations

Every symbol reports **zero blocking provider failures and zero optional capture failures**. The 390 changed Parquets are the provider summaries' changes, separate from calculated output counts.

| Symbol | Changed provider Parquets | Fundamental outputs | Technical outputs | Local advisories |
|---|---:|---:|---:|---:|
| AAPL | 60 | 4 | 35 | 2 |
| AMZN | 33 | 4 | 35 | 0 |
| GOOG | 33 | 4 | 35 | 0 |
| MU | 33 | 4 | 35 | 0 |
| NVDA | 33 | 4 | 35 | 0 |
| SNDK | 33 | 4 | 35 | 0 |
| COST | 33 | 4 | 29 | 0 |
| CROX | 33 | 4 | 35 | 0 |
| PATH | 33 | 4 | 35 | 0 |
| TWST | 33 | 4 | 35 | 0 |
| IONQ | 33 | 4 | 35 | 0 |

All eleven fundamental runs, eleven technical runs and eleven signal runs report `status=ok`. Fundamental outputs total **44** and technical outputs total **379**, with zero calculation failures or not-ready skips. All **456 distinct logged stock Parquet output paths** exist.

AAPL's two shared local advisories are the previously examined **Databento CME stale partition-derived context** and **FMP retained historical clock-skew rejection**. Current CME context MBP remains explicitly saturated; the current FMP quote passes the native pure calculation. These optional quality limitations remain disclosed and unchanged. Details and exact diagnostic/source paths are in [provider-preflight-review.md](C:/dev/ducketz/artifacts/analysis/overnight-20260918/provider-preflight-review.md).

This audit verifies current provider/native acquisition evidence only. It does not claim that every optional feature family or downstream model qualifies, nor does operational EQUS.MINI data replace the separately required XNAS.ITCH target-history stage. No provider/broker calls, supervision claims, process controls, training or production changes were made.

Full evidence: [provider-completion.json](C:/dev/ducketz/artifacts/analysis/overnight-20260918/provider-completion.json). Native log: [loop_a_close_fetch.log](C:/DATASTORE/ml/overnight-runs/20260918T040804.048018Z/loop_a_close_fetch.log).
