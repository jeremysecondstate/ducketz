# 2026-09-24 provider evidence audit

Observed **2026-09-24T05:55:34.483653+00:00**. Status: **PASS_NATIVE_LOOP_A_AND_33_OPRA_SCOPES_COMPLETE**.

Native run `C:/DATASTORE/ml/overnight-runs/20260924T044352.904210Z` prepares **2026-09-24** from source session **2026-09-23**, with original deadline `2026-09-24T11:00:00+00:00`. Its saved/current universe agrees on 11 symbols: AAPL, AMZN, GOOG, MU, NVDA, SNDK, COST, CROX, PATH, TWST, IONQ.

Base cycle `20260924T044353.640845Z-pid64532` is **COMPLETE**, with all five configured providers. Current log has 11/11 symbol summaries, 11/11 fundamental completions, 11/11 technical completions and 11/11 signal completions. 456 logged stock Parquet paths were checked; 0 are missing.

Provider changes total **180 Parquets**. Calculations produced **44 fundamental** and **379 technical outputs**, with **0 failures** and **0 technical not-ready skips**. Every observed symbol capture summary has zero blocking and optional capture failures.

Production OPRA: **33/33** fresh exact symbol/schema preflights, **33/33** current-run exclusive `2026-09-24` cursors, **33/33** source-session Historical partition receipts, and **33/33** native COMPLETE scope log entries. Available receipts are checked for exact source/range, checksum binding, zero-dollar nested/total cost and native capacity formula. Observed estimated download size: **3,594,568,328 bytes** (20,000,000,000-byte ceiling). Current partition raw/normalized files hashed: **66**.

Native Loop A stage completion: **COMPLETE**. Native OPRA terminal summary: **present**. Health inventory freshness: **True**; inventory totals consistent: **True**. Pending native work is not a provider failure.

Discrepancies: **0**. Outstanding evidence checks: **0**.

Prior optional advisory evidence is retained at [provider-preflight-review.md](C:/dev/ducketz/artifacts/analysis/overnight-20260918/provider-preflight-review.md). The previous CME stale partition context and FMP historical clock-skew rejections do not establish a new defect; current evidence must still be compared before claiming unchanged status.

This is bounded provider evidence only. Operational EQUS.MINI captures do not replace the later XNAS.ITCH target-history stage. Selected native health inventory is not proof that every retained historical directory is valid. No provider/broker requests, source writes, process changes, supervision claims or training were performed.

Full evidence: [provider-completion.json](C:/dev/ducketz/artifacts/analysis/overnight-20260924/resumed-provider/provider-completion.json).

## Current and retained optional advisories

Disposition: **REVIEWED_OPTIONAL_SOURCE_LIMITATIONS_NO_CAPTURE_FAILURE**. The original automatic review flag and exact diagnostics are retained in the JSON; the evidence-based review is recorded below.

- The current CME message has a greater age for the same stale September 3 candidate. The source-selection mismatch and current capture limits explain the exclusion; no unresolved capture failure or exact unchanged-message claim remains.

## Explicit CME scope coverage

Successful requests do not prove every requested symbol returned observations. Current capture rows are distinguished from retained history; sparse observed bars are not labeled as download failures.

| Scope | Schema | Current rows | Missing configured symbols |
|---|---|---:|---|
| CME_CONTEXT | ohlcv-1m | 150 | None |
| CME_CONTEXT | bbo-1m | 200 | None |
| CME_CONTEXT | mbp-10 | 3,321 | None |
| CME_CONTRACTS | ohlcv-1m | 120 | None |
| CME_CONTRACTS | bbo-1m | 160 | None |
| CME_CONTRACTS | mbp-10 | 2,899 | None |

CME MBP request limits remain explicit bounded evidence. No missing raw contract is substituted with continuous or another contract price.

## Resumed attempt and optional-source review

This successful audit belongs only to resumed run `20260924T044352.904210Z`, which completed Loop A at 05:54:52 UTC after fresh Schwab authorization. All eleven native Schwab option captures succeeded; no `invalid_grant` appears in this resumed log. The stopped original run remains CANCELLED, and its partial provider evidence was preserved separately. No downstream publication completion is inferred.

The automatic CME note flags the changing age in the exact advisory text. Its source cause remains the already documented partition-shadowing/group-alias mismatch; the September 3 candidate stays excluded. All six resumed direct CME scopes contain every configured symbol, with no unresolved-symbol warnings. Resumed MBP captures are now explicitly unsaturated after narrowing, with 3,321 continuous and 2,899 literal-contract rows around the prior session's 20:48 UTC boundary. They are not full-interval depth coverage. Current-stage aggregate row counts represent newly stored rows after deduplication; they need not equal the native provider response counts.

FMP's new current quote independently passes its pure derivation. The retained full source still rejects the same 9.471-second historical quote/receipt clock skew against its five-second gate; no raw rows were removed or adjusted. These explained optional exclusions do not change this provider-completion pass.

The audit performed no provider/broker calls, claims, production writes, new training or trading operations. Root remains responsible for downstream supervision.
