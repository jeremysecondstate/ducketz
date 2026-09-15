# OPRA current-session replay audit

Audited 2026-09-15T04:32:48.932342+00:00. Status: COMPLETE_CURRENT_SESSION_METADATA.

- Current-session metadata checks passed for 33/33 production symbol/schema scopes; required exclusive cursor end is September 15, covering the September 14 session.
- Historical metadata observed at 2026-09-15T04:27:34.266921+00:00 ended at September 14 14:10 UTC for hourly bars and 16:20 UTC for CBBO/definitions. The native complete-day limit was therefore September 14. Existing cursors had already reached that limit, explaining 33 requested but zero eligible Historical preflight/download scopes.
- The existing owner used the documented OPRA.PILLAR Live replay. Hourly bars/definitions request September 14 UTC midnight through September 15 UTC midnight; CBBO requests 12:30 UTC through midnight. This covers the complete regular options session.
- Completed deliveries have exact scope identities, subscription acknowledgements, replay-completion records, zero provider/callback errors, no reconnects, matching cursor/manifest/receipt bindings, and matching saved file sizes. Metadata errors: 0. Native replay failure lines: 0.
- Recorded current replay raw bytes: 1,236,185,072, within the native 20,000,000,000-byte run limit. No Historical acquisition cost is inferred from Live byte counts.
- This audit checked current-session metadata and bindings only; the native owner performs payload verification. It made no provider/broker calls, retries, process/claim actions, or production changes. No XNAS.ITCH Live route is involved.

Detailed [evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260915/opra-replay-audit.json); [native log](C:/DATASTORE/ml/overnight-runs/20260915T040742.365640Z/loop_a_close_fetch.log).
