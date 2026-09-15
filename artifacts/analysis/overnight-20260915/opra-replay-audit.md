# OPRA current-session replay audit

Audited 2026-09-15T05:18:36.162686+00:00. Status: NATIVE_LOOP_A_AND_OPRA_COMPLETE.

- Current-session metadata checks passed for 33/33 production symbol/schema scopes; required exclusive cursor end is September 15, covering the September 14 session.
- Historical metadata observed at 2026-09-15T04:27:34.266921+00:00 ended at September 14 14:10 UTC for hourly bars and 16:20 UTC for CBBO/definitions. The native complete-day limit was therefore September 14. Existing cursors had already reached that limit, explaining 33 requested but zero eligible Historical preflight/download scopes.
- The existing owner used the documented OPRA.PILLAR Live replay. Hourly bars/definitions request September 14 UTC midnight through September 15 UTC midnight; CBBO requests 12:30 UTC through midnight. This covers the complete regular options session.
- Completed deliveries have exact scope identities, subscription acknowledgements, replay-completion records, zero provider/callback errors, no reconnects, matching cursor/manifest/receipt bindings, and matching saved file sizes. Metadata errors: 0. Native replay failure lines: 0.
- Recorded current replay raw bytes: 1,236,185,072, within the native 20,000,000,000-byte run limit. No Historical acquisition cost is inferred from Live byte counts.
- This audit checked current-session metadata and bindings only; the native owner performs payload verification. It made no provider/broker calls, retries, process/claim actions, or production changes. No XNAS.ITCH Live route is involved.

Detailed [evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260915/opra-replay-audit.json); [native log](C:/DATASTORE/ml/overnight-runs/20260915T040742.365640Z/loop_a_close_fetch.log).

## Native completion evidence

- Loop A completed at 2026-09-15T05:16:09.958768+00:00 with exit code 0. Native OPRA summary confirms 33 requested/completed, zero failed/blocked/deferred/bootstrap scopes, 33 replay completions, 1,236,185,072 captured bytes, and zero selected Historical acquisition cost.
- Fresh [OPRA health](C:/DATASTORE/market-data/databento/opra/OPRA.PILLAR/health/current.json) observed 2026-09-15T05:15:55.198554+00:00 includes 66,284 verified selected partitions. Production-schema latest observations reach September 14.
- Failed partitions listed in this health: **0**. The native health format has no failure list/count, and its iterator skips failed verification; total retained archive failures therefore remain **unavailable**, rather than proven zero. This audit did not rescan or rehash that archive.
- Other schema histories still ending September 11 are retained research scopes, outside production freshness requirements. The known historical optional Pricing feature quarantine is a separate feature-quality issue; neither is counted as a failed current OPRA scope.

