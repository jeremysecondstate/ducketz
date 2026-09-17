# OPRA preflight audit — September 17 preparation

Observed **2026-09-17T04:31:49.632073+00:00**. All **33 exact production symbol/schema scopes** were freshly preflighted: 11 symbols × OHLCV-1h, CBBO-1m and definition. Their requested ranges all cover source session September 16 through the exclusive September 17 boundary. No scopes are missing or extra.

Every per-scope and nested schema estimate is **$0**, and all cost estimates are complete. Total estimated download size is **3,640,230,272 bytes**, below the native 20,000,000,000-byte run ceiling. All semantic checksums verify. Every capacity receipt passes with the correct **5 GiB + 2 × estimated bytes** formula; minimum observed free space is **1,330,971,799,552 bytes** and largest single-scope requirement is **7,319,538,720 bytes**.

The native log confirms `requested_scopes=33`, `preflighted_scopes=33`, `selected_scopes=33`, `deferred_scopes=0`, `selected_estimated_cost_usd=0.0`. Its `$1.00` header is the existing aggregate selection ceiling; it is not an actual request quote or charge. No nonzero acquisition is authorized by this audit.

Downloads and native verification are now underway. At observation **3/33 cursors** had reached the required exclusive September 17 boundary; completed required-session coverage remains a final supervision check. These preflight results establish requested coverage and acquisition eligibility, not completion of downloads.

Only local metadata and logs were read. No provider/broker calls, archive scans, process controls, supervision claims or production changes were made.

Evidence: [complete receipt audit](C:/dev/ducketz/artifacts/analysis/overnight-20260917/opra-preflight-progress.json), [Loop A log](C:/DATASTORE/ml/overnight-runs/20260917T040722.433471Z/loop_a_close_fetch.log).
