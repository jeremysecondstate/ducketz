# September 19 provider evidence audit

Observed **2026-09-19T04:57:26.747665+00:00**. Status: **PASS_NATIVE_LOOP_A_AND_33_OPRA_SCOPES_COMPLETE**.

Native run `C:/DATASTORE/ml/overnight-runs/20260919T040748.657031Z` prepares **2026-09-21** from source session **2026-09-18**, with original deadline `2026-09-21T11:00:00+00:00`. Its saved/current universe agrees on eleven symbols: AAPL, AMZN, GOOG, MU, NVDA, SNDK, COST, CROX, PATH, TWST, IONQ.

Base cycle `20260919T040749.499463Z-pid53472` is **COMPLETE**, with all five configured providers. Current log has 11/11 symbol summaries, 11/11 fundamental completions, 11/11 technical completions and 11/11 signal completions. 456 logged stock Parquet paths were checked; 0 are missing.

Provider changes total **390 Parquets**. Calculations produced **44 fundamental** and **379 technical outputs**, with **0 failures** and **0 technical not-ready skips**. Every observed symbol capture summary has zero blocking and optional capture failures.

Production OPRA: **33/33** fresh exact symbol/schema preflights, **33/33** current-run exclusive `2026-09-19` cursors, **33/33** source-session Historical partition receipts, and **33/33** native COMPLETE scope log entries. Available receipts are checked for exact source/range, checksum binding, zero-dollar nested/total cost and native capacity formula. Observed estimated download size: **4,875,287,440 bytes** (20,000,000,000-byte ceiling). Current partition raw/normalized files hashed: **66**.

All 33 native scope lines have distinct exact production symbol/schema identities and COMPLETE status. The guarded selection summary agrees with all 33 receipts, defers none and selects exactly **$0** in estimated cost. Minimum available space was **1,335,310,213,120 bytes**; largest per-scope requirement was **7,982,766,560 bytes**, with zero shortfall under the native 5 GiB + 2 x estimated bytes formula.

The **66 current-session raw/normalized file hashes** all match both manifest and receipt bindings; local sizes match. These files total **560,035,316 bytes** and contain **15,128,901 normalized rows**. Every Historical partition has exact September 18 00:00 through September 19 00:00 UTC request bounds, OPRA.PILLAR parent identity and timeseries-stream delivery. No other archive partition was read or hashed.

Native Loop A stage is **COMPLETE, exit 0**, at **2026-09-19T05:41:07.959067+00:00**. The final OPRA summary verifies **33 requested/preflighted/completed**, zero capacity-blocked/failed/bootstrap-required/deferred scopes, zero Live replay scopes/bytes, the same **4,875,287,440-byte** estimate and exactly **$0** selected estimated cost. Native maintenance exits 0 for September 19.

Discrepancies: **0**. Outstanding provider evidence checks: **0**.

Current optional advisories were reconciled against [prior evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260918/provider-preflight-review.md); the detailed current review below establishes the repeated limitations with unchanged native gates.

This is bounded provider evidence only. Operational EQUS.MINI captures do not replace the later XNAS.ITCH target-history stage. Selected native health inventory is not proof that every retained historical directory is valid. No provider/broker requests, source writes, process changes, supervision claims or training were performed.

Full evidence: [provider-completion.json](C:/dev/ducketz/artifacts/analysis/overnight-20260919/provider-completion.json).

## Current optional advisory review

Observed **2026-09-19T04:28:23.344092+00:00**. **Known optional limitations; no new actionable provider defect established.**

CME's fresh diagnostic at `2026-09-19 04:21:01.836679+00:00` preserves `advisory`, `CmeCrossAssetQualityError`, persisted-only inputs and original provider rows. It again rejects the September 3 21:00 UTC candidate under the same 15-minute rule: `CME cross-asset context rejected every candidate window: 2026-09-03T21:00:00+00:00: CME BBO NQ is stale by 15 days 07:21:02.684629413; maximum is 0 days 00:15:00`. This matches the prior source-bound rejection; the additional elapsed day changes the age only.

| Current CME scope | OHLCV rows | BBO rows | MBP rows | MBP saturated |
|---|---:|---:|---:|---|
| CME_CONTEXT | 4490 | 310 | 4998 | True |
| CME_CONTRACTS | 2503 | 126 | 2438 | False |

All six current CME requests succeeded in the native log. Saturated MBP remains capped evidence even when normalization retains fewer than 5,000 rows. The JSON preserves each exact request range, observed market timestamps, shrink count and saturation field. Both BBO receipts explicitly report BACKTRACKED with two empty-window expansions, moving the effective start from September 18 23:00 to 20:00 UTC; latest observations are September 18 20:59:59 UTC. OHLCV receipts shrink twice to September 18 06:00 through September 19 00:00 UTC and end at September 18 20:59 UTC. These bounded provider windows are disclosed, not complete interval coverage. Current captures do not qualify the rejected historical partition-derived context.

The FMP diagnostic remains the single retained September 2 clock-skew advisory with **9.471 seconds** against the unchanged **5-second** maximum; no current-cycle diagnostic row appeared. The 1,490-row saved quote history still reproduces that exact rejection in the native pure calculation, with the same five historical offenders. The **current-cycle quote passes** that calculation in memory with one derived row. Its receipt is `2026-09-19 04:21:12.856643+00:00`, provider market timestamp `2026-09-18 20:00:00+00:00`, and explicit USO ETF-share-price proxy identity. The single current row has no preceding same-chain observation, so its return remains unavailable; this does not alter or qualify the retained historical history.

Preserve both optional derivation gates and source evidence. These repeated advisories do not justify provider retries, restarting the pipeline, deleting source rows or weakening quality checks.

## Native health completion

Terminal reconciliation at **2026-09-19T05:42:43.377412+00:00** preserves the original **2026-09-19T04:57:26.747665+00:00** verification of the 66 current-session data files. The pipeline has advanced to **loop_b_directional_generation** under the same owner and original September 21 04:00 Pacific deadline; this report establishes provider-stage completion, not whole-overnight completion.

Fresh native health is observed at **2026-09-19T05:40:47.218636+00:00** and contains **66,416 selected verified partitions**, **4,872,353,835 rows**, **64,243,607,356 normalized bytes** and **81,535,012,770 raw bytes**. Schema sums match all four totals. Production schema counts are `ohlcv-1h`: **14,575 partitions / 159,737,873 rows**; `cbbo-1m`: **1,289 partitions / 781,719,709 rows**; `definition`: **7,244 partitions / 6,700,934 rows**.

Before completion, exact native process lineage and a retained OPRA file handle supported the expected [health refresh call](C:/dev/ducketz/datafetching/options_runtime.py:1249). Last active-child snapshots `2026-09-19T05:38:54.024522+00:00` through `2026-09-19T05:40:24.098574+00:00` show CPU advancing **69.719 seconds** and I/O **5,890,405,435 bytes**, with zero issues. Native [publish_health](C:/dev/ducketz/datafetching/databento_opra_history.py:903) atomically publishes only after iterating/validating its selected inventory, explaining the quiet log and unchanged previous health JSON during that phase. Descendant-summed counters decrease after child exit; that is an expected process-set transition. No restart was needed.

The health inventory omits skipped/invalid retained directories and is not proof that every archive directory is valid. Final reconciliation read only terminal log/stage metadata, the small health JSON and recorded process metrics. It performed no whole-archive scan or repeated data hashes, made no provider/broker calls, and preserved source files, quality gates and all prior advisory evidence.
