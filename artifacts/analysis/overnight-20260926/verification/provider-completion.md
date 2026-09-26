# 2026-09-28 provider evidence audit

Observed **2026-09-26T06:44:31.327161+00:00**. Status: **PASS_NATIVE_LOOP_A_AND_33_OPRA_SCOPES_COMPLETE**.

Native run `C:/DATASTORE/ml/overnight-runs/20260926T040723.834511Z` prepares **2026-09-28** from source session **2026-09-25**, with original deadline `2026-09-28T11:00:00+00:00`. Its saved/current universe agrees on 11 symbols: AAPL, AMZN, GOOG, MU, NVDA, SNDK, COST, CROX, PATH, TWST, IONQ.

Base cycle `20260926T040724.729021Z-pid46624` is **COMPLETE**, with all five configured providers. Current log has 11/11 symbol summaries, 11/11 fundamental completions, 11/11 technical completions and 11/11 signal completions. 456 logged stock Parquet paths were checked; 0 are missing.

Provider changes total **391 Parquets**. Calculations produced **44 fundamental** and **379 technical outputs**, with **0 failures** and **0 technical not-ready skips**. Every observed symbol capture summary has zero blocking and optional capture failures.

Production OPRA: **33/33** fresh exact symbol/schema preflights, **33/33** current-run exclusive `2026-09-26` cursors, **33/33** source-session Historical partition receipts, and **33/33** native COMPLETE scope log entries. Available receipts are checked for exact source/range, checksum binding, zero-dollar nested/total cost and native capacity formula. Observed estimated download size: **4,854,402,936 bytes** (20,000,000,000-byte ceiling). Current partition raw/normalized files hashed: **66**.

Native Loop A stage completion: **COMPLETE**. Native OPRA terminal summary: **present**. Health inventory freshness: **True**; inventory totals consistent: **True**. Pending native work is not a provider failure.

Discrepancies: **0**. Outstanding evidence checks: **0**.

Prior optional advisory evidence is retained at [provider-preflight-review.md](C:/dev/ducketz/artifacts/analysis/overnight-20260918/provider-preflight-review.md). The previous CME stale partition context and FMP historical clock-skew rejections do not establish a new defect; current evidence must still be compared before claiming unchanged status.

This is bounded provider evidence only. Operational EQUS.MINI captures do not replace the later XNAS.ITCH target-history stage. Selected native health inventory is not proof that every retained historical directory is valid. No provider/broker requests, source writes, process changes, supervision claims or training were performed.

Full evidence: [provider-completion.json](C:/dev/ducketz/artifacts/analysis/overnight-20260926/verification/provider-completion.json).

## Current and retained optional advisories

Disposition: **ADVISORY_REVIEW_REQUIRED**. Exact current/retained diagnostics and pure FMP derivation are saved in the JSON.

- cme retains prior diagnostic category/policy, but exact message differs; no unchanged-source claim

## Explicit CME scope coverage

Successful requests do not prove every requested symbol returned observations. Current capture rows are distinguished from retained history; sparse observed bars are not labeled as download failures.

| Scope | Schema | Current rows | Missing configured symbols |
|---|---|---:|---|
| CME_CONTEXT | ohlcv-1m | 4,499 | None |
| CME_CONTEXT | bbo-1m | 310 | None |
| CME_CONTEXT | mbp-10 | 4,996 | None |
| CME_CONTRACTS | ohlcv-1m | 3,600 | None |
| CME_CONTRACTS | bbo-1m | 248 | None |
| CME_CONTRACTS | mbp-10 | 4,996 | None |

CME MBP request limits remain explicit bounded evidence. No missing raw contract is substituted with continuous or another contract price.
