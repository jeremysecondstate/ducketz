# IONQ and PATH planning references remain unavailable

Audited 2026-09-15T05:54:05.145062+00:00. This is an intentional native source-quality guard; no current acquisition failure or concrete code defect was established.

| Symbol | Last observed minute close | Boundary gap | Recorded undefined native rows |
|---|---|---:|---|
| IONQ | September 14, 23:47 UTC | 13 minutes | 3, on September 19, 2022 at 09:06, 09:08 and 09:32 UTC |
| PATH | September 14, 23:32 UTC | 28 minutes | 1, on October 26, 2021 at 22:20 UTC |

Both gaps fit the 240-minute after-hours policy. Their fresh September 10�15 XNAS.ITCH partitions cover the exact origin through September 15 00:00 UTC, were published before the planning cutoff, contain no provider warnings and have zero undefined price rows. Bounded current manifest/receipt and normalized checks pass; the native coverage helper accepts the saved coverage metadata for both.

The existing completion code first checks **any undefined-price count in the symbol's loaded history**. IONQ's count is 3 and PATH's is 1, so it returns `UNDEFINED_NATIVE_PRICE_OBSERVATIONS` before checking coverage. This is explicitly required by the existing regression test. Consequently `source_coverage: null` means the check was not reached; it does not show an incomplete current download.

All 28 affected hourly path points retain `UNAVAILABLE_REFERENCE_PRICE`. The shared direction ledger correctly records `UNAVAILABLE_PRICE_REFERENCES`, with no events, hourly cash projection, ending cash summary or projected ending holdings. No prices or synthetic rows were created by this audit.

Detailed [evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260915/price-availability-audit.json); [saved completion](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260915T054710.644968Z/planning-reference-completion.json); [native guard](C:/dev/ducketz/ml/gameplan_price_completion.py:253); [regression test](C:/dev/ducketz/tests/test_gameplan_price_completion.py:278).
