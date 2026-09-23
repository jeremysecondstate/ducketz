# 2026-09-22 provider evidence audit

Observed **2026-09-22T05:23:09.828686+00:00**. Status: **PASS_NATIVE_LOOP_A_AND_33_OPRA_SCOPES_COMPLETE**.

Native run `C:/DATASTORE/ml/overnight-runs/20260922T040810.607319Z` prepares **2026-09-22** from source session **2026-09-21**, with original deadline `2026-09-22T11:00:00+00:00`. Its saved/current universe agrees on 11 symbols: AAPL, AMZN, GOOG, MU, NVDA, SNDK, COST, CROX, PATH, TWST, IONQ.

Base cycle `20260922T040811.499513Z-pid36676` is **COMPLETE**, with all five configured providers. Current log has 11/11 symbol summaries, 11/11 fundamental completions, 11/11 technical completions and 11/11 signal completions. 456 logged stock Parquet paths were checked; 0 are missing.

Provider changes total **389 Parquets**. Calculations produced **44 fundamental** and **379 technical outputs**, with **0 failures** and **0 technical not-ready skips**. Every observed symbol capture summary has zero blocking and optional capture failures.

Production OPRA: **33/33** fresh exact symbol/schema preflights, **33/33** current-run exclusive `2026-09-22` cursors, **33/33** source-session Historical partition receipts, and **33/33** native COMPLETE scope log entries. Available receipts are checked for exact source/range, checksum binding, zero-dollar nested/total cost and native capacity formula. Observed estimated download size: **4,858,533,552 bytes** (20,000,000,000-byte ceiling). Current partition raw/normalized files hashed: **66**.

Native Loop A stage completion: **COMPLETE**. Native OPRA terminal summary: **present**. Health inventory freshness: **True**; inventory totals consistent: **True**. Health observed `2026-09-22T05:22:05.914711+00:00` before stage completion `2026-09-22T05:22:22.267006+00:00`; **66,449** selected partitions and all schema totals reconcile.

Discrepancies: **0**. Outstanding evidence checks: **0**.

Current optional derivation evidence was compared with the prior saved audit; the separate new raw-contract coverage gap is disclosed below. No archive hashes were repeated during that review.

This is bounded provider evidence only. Operational EQUS.MINI captures do not replace the later XNAS.ITCH target-history stage. Selected native health inventory is not proof that every retained historical directory is valid. No provider/broker requests, source writes, process changes, supervision claims or training were performed.

Full evidence: [provider-completion.json](C:/dev/ducketz/artifacts/analysis/overnight-20260922/provider-completion.json).

## Current and retained optional advisories

The fresh CME advisory rejects the same September 3 21:00 UTC historical candidate under the unchanged 15-minute BBO freshness gate. Comparison with the prior saved message shows only the elapsed stale age increased, from 15 to 18 days. The diagnostic still records advisory severity, persisted inputs only and original provider rows preserved. This is separate from current raw-contract scope coverage.

The retained FMP diagnostic remains the same September 2 **9.471-second** clock-skew rejection against the **5-second** limit. No current diagnostic row was added. Pure local validation of the current native USO proxy quote passes with one derived row; the 1,491-row retained history still reproduces the prior rejection. Historical evidence and gates remain unchanged.

## New explicit CME raw-contract coverage gap

The native log newly reports unresolved **ESU6 and NQU6**. The explicit configuration retains these fixed raw symbols alongside CLV6 and GCZ6; the provider does not roll the configured contract list automatically. All three current raw captures contain only CLV6 and GCZ6. Each request completed successfully, which does not establish that every requested contract returned observations.

The separate continuous context contains all five configured roots—ES, NQ, RTY, CL and GC—in OHLCV, BBO and MBP captures. The cross-asset derivation explicitly filters continuous symbols. Missing raw contracts therefore remain a separate source limitation; they were not silently replaced with continuous or another contract's prices. MBP requests remain explicitly capped evidence.

| Scope | Schema | Current rows | Missing configured symbols |
|---|---|---:|---|
| CME_CONTEXT | ohlcv-1m | 2,384 | None |
| CME_CONTEXT | bbo-1m | 300 | None |
| CME_CONTEXT | mbp-10 | 4,999 | None |
| CME_CONTRACTS | ohlcv-1m | 2,788 | ESU6, NQU6 |
| CME_CONTRACTS | bbo-1m | 90 | ESU6, NQU6 |
| CME_CONTRACTS | mbp-10 | 4,093 | ESU6, NQU6 |

Exact native warning lines, current source paths/ranges, nonsecret configuration keys and code boundaries are preserved in [CME raw-scope evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260922/cme-raw-scope-review.json). The log is [loop_a_close_fetch.log](C:/DATASTORE/ml/overnight-runs/20260922T040810.607319Z/loop_a_close_fetch.log:146). This review does not independently establish contract expiry dates. No configuration change, unchanged retry, provider request or restart was performed. A future rollover must verify the intended replacement source identities.
