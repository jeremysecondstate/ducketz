# XNAS.BASIC prospective coverage validation

Verified 120 XNYS sessions, 2026-03-23 through 2026-09-11, plus prior-session close (2026-03-20). All seven exact requests passed $0 cost preflight before acquisition; total estimated billable size 43,772,232 bytes. Each native DBN was decoded again and reproduced its saved parquet exactly. All per-symbol metadata, request scopes, row counts and checksums passed.

This is coverage evidence only. It does not switch a production source, revise frozen forecasts, train models, change controls or submit orders.

The comparison uses native five-minute selectors: opens at/after each opening clock and completed closes at/before each closing clock. There are 26 unique open/close boundary checks per session and 19 entry windows. Future weekly and overnight endpoints after September 11 are excluded from matured sample counts.

| Symbol | XNAS.ITCH missing boundaries | XNAS.BASIC missing boundaries | BASIC positive-volume missing | ITCH available entry windows | BASIC positive-volume available |
| --- | ---: | ---: | ---: | ---: | ---: |
| AAPL | 1/3120 | 0/3120 | 0/3120 | 2274/2275 | 2275/2275 |
| AMZN | 0/3120 | 0/3120 | 0/3120 | 2275/2275 | 2275/2275 |
| GOOG | 34/3120 | 0/3120 | 0/3120 | 2225/2275 | 2275/2275 |
| MU | 0/3120 | 0/3120 | 0/3120 | 2275/2275 | 2275/2275 |
| NVDA | 1/3120 | 0/3120 | 0/3120 | 2274/2275 | 2275/2275 |
| SNDK | 0/3120 | 0/3120 | 0/3120 | 2275/2275 | 2275/2275 |
| COST | 547/3120 | 7/3120 | 48/3120 | 1569/2275 | 2199/2275 |

Provider quality metadata marks August 31 XNAS.BASIC degraded; XNAS.ITCH has no degraded dates in this comparison. The degraded date is retained transparently in raw files. A separate conservative sensitivity excludes that action date and any entry target touching it, and excludes all native zero-volume bars. The same date/window exclusions are applied to the ITCH baseline for a matched comparison. This affects eligibility; complete raw delivery does not establish good provider quality on that date.

| Symbol | ITCH missing boundaries, matched dates | Conservative BASIC missing boundaries | ITCH available entry windows, matched dates | Conservative BASIC available entry windows | Native BASIC zero-volume rows (whole requested range) |
| --- | ---: | ---: | ---: | ---: | ---: |
| AAPL | 1/3094 | 0/3094 | 2250/2251 | 2251/2251 | 659 |
| AMZN | 0/3094 | 0/3094 | 2251/2251 | 2251/2251 | 240 |
| GOOG | 34/3094 | 0/3094 | 2201/2251 | 2251/2251 | 453 |
| MU | 0/3094 | 0/3094 | 2251/2251 | 2251/2251 | 5 |
| NVDA | 1/3094 | 0/3094 | 2250/2251 | 2251/2251 | 3 |
| SNDK | 0/3094 | 0/3094 | 2251/2251 | 2251/2251 | 12 |
| COST | 538/3094 | 47/3094 | 1558/2251 | 2177/2251 | 10,967 |

Zero-volume native records are not relabeled as fabricated or no-trade observations. Their provider semantics remain unresolved. The positive-volume comparison proves which gains survive without them. Existing XNAS.ITCH rows have zero such records across all seven symbols; see the independent baseline audit.

Per-symbol regular/premarket/afterhours missing rates and all 19 individual route counts are retained in comparison.json and positive-volume-sensitivity.json; row-level evidence is in boundary-results.parquet and entry-window-results.parquet. Native data, preflight, metadata and manifests reside in each symbol directory. provider-conditions.json retains provider quality evidence.

Before any prospective adoption: approve an explicit XNAS.BASIC source identity; enforce data-quality admission rules; determine native zero-volume semantics; validate the same-source historical labels/features, chronological cohort minimums and model assessments. Frozen XNAS.ITCH reports keep their source, and no cross-dataset gap substitution is authorized by this audit.

## Missing boundary rates by market period

Each cell shows XNAS.ITCH missing/total (percent) → positive-volume XNAS.BASIC missing/total (percent), before excluding the degraded date.

| Symbol | Premarket | Regular session | Afterhours |
| --- | --- | --- | --- |
| AAPL | 0/600 (0.00%) → 0/600 (0.00%) | 0/1560 (0.00%) → 0/1560 (0.00%) | 1/960 (0.10%) → 0/960 (0.00%) |
| AMZN | 0/600 (0.00%) → 0/600 (0.00%) | 0/1560 (0.00%) → 0/1560 (0.00%) | 0/960 (0.00%) → 0/960 (0.00%) |
| GOOG | 1/600 (0.17%) → 0/600 (0.00%) | 0/1560 (0.00%) → 0/1560 (0.00%) | 33/960 (3.44%) → 0/960 (0.00%) |
| MU | 0/600 (0.00%) → 0/600 (0.00%) | 0/1560 (0.00%) → 0/1560 (0.00%) | 0/960 (0.00%) → 0/960 (0.00%) |
| NVDA | 0/600 (0.00%) → 0/600 (0.00%) | 0/1560 (0.00%) → 0/1560 (0.00%) | 1/960 (0.10%) → 0/960 (0.00%) |
| SNDK | 0/600 (0.00%) → 0/600 (0.00%) | 0/1560 (0.00%) → 0/1560 (0.00%) | 0/960 (0.00%) → 0/960 (0.00%) |
| COST | 171/600 (28.50%) → 9/600 (1.50%) | 0/1560 (0.00%) → 0/1560 (0.00%) | 376/960 (39.17%) → 39/960 (4.06%) |

## All 19 entry-window routes

Each cell shows XNAS.ITCH available/mature → positive-volume XNAS.BASIC available/mature, before excluding the degraded date. This measures price endpoint availability, not fitted-model qualification or feature-cohort admission.

| Route | AAPL | AMZN | GOOG | MU | NVDA | SNDK | COST |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | 120/120 → 120/120 | 120/120 → 120/120 | 119/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 65/120 → 115/120 |
| 1h@05:00 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 57/120 → 117/120 |
| 1h@06:00 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 94/120 → 120/120 |
| 1h@07:00 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 |
| 1h@08:00 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 |
| 1h@09:00 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 |
| 1h@10:00 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 |
| 1h@11:00 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 |
| 1h@12:00 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 |
| 1h@13:00 | 120/120 → 120/120 | 120/120 → 120/120 | 118/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 73/120 → 119/120 |
| 1h@14:00 | 119/120 → 120/120 | 120/120 → 120/120 | 108/120 → 120/120 | 120/120 → 120/120 | 119/120 → 120/120 | 120/120 → 120/120 | 46/120 → 112/120 |
| 1h@15:00 | 120/120 → 120/120 | 120/120 → 120/120 | 109/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 45/120 → 107/120 |
| 1h@16:00 | 120/120 → 120/120 | 120/120 → 120/120 | 113/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 30/120 → 106/120 |
| 4h@04:00 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 101/120 → 118/120 |
| 4h@08:00 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 |
| 4h@12:00 | 120/120 → 120/120 | 120/120 → 120/120 | 113/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 62/120 → 111/120 |
| 4h@16:00 | 119/119 → 119/119 | 119/119 → 119/119 | 115/119 → 119/119 | 119/119 → 119/119 | 119/119 → 119/119 | 119/119 → 119/119 | 56/119 → 108/119 |
| 1d@D+1 | 120/120 → 120/120 | 120/120 → 120/120 | 117/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 120/120 → 120/120 | 50/120 → 115/120 |
| 1w@D+5 | 116/116 → 116/116 | 116/116 → 116/116 | 113/116 → 116/116 | 116/116 → 116/116 | 116/116 → 116/116 | 116/116 → 116/116 | 50/116 → 111/116 |
