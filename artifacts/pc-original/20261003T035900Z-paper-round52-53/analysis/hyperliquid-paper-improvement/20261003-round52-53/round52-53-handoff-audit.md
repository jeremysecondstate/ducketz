# Hyperliquid Paper Improvement — Round 52 to Round 53 handoff

**Run:** Atlas / `hyperliquid-paper-improvement`  
**Captured:** 2026-10-03 04:02:17 UTC (2026-10-02 PDT)  
**Repository:** `C:/dev/ducketz`, local branch `main`, local base `99532621db0d42dfe8c655300cc93abda81240d5`; verified public `origin/main` base `496f533197bda4e706a6f7b90fb7808e64b35bc3`.  
**Application source/config changes:** none.

## Round 52 comparison

Round 52 was a one-hour comparison seeded 2026-10-03T01:40:54.468557358+00:00 and due 2026-10-03T02:40:54.468557+00:00. Paper was observed at 2026-10-03T03:27:34.804003+00:00, 46.67 minutes after due; the latest public-account observation was 2026-10-03T03:27:48.857547+00:00. Native cadence assessed **LATE_UNSCORED**, retaining one hour. The comparison passed same-account/common-mark checks, mirror-baseline reconciliation, complete empty external-flow histories and 14.433s observation skew (120s cap), but timing invalidated the score.

At common marks, Paper closed at **$44,284.01** against **$44,285.67** for the actual accounts, an all-in edge of **$-1.65**. Paper return was **-0.101625%**, actual return **-0.097892%**, excess **-0.003733 percentage points**. This negative edge is descriptive only because the endpoint was late; it is not a LOSS.

| Account | Opening | Paper common mark | Actual common mark | Paper return | Actual return | Paper minus actual |
|---|---:|---:|---:|---:|---:|---:|
| alex | $6,518.52 | $6,524.22 | $6,587.19 | +0.0875% | +1.0534% | $-62.96 |
| clearpond | $32,906.11 | $32,872.96 | $32,848.09 | -0.1007% | -0.1763% | $+24.87 |
| jeremy | $4,904.43 | $4,886.82 | $4,850.39 | -0.3591% | -1.1020% | $+36.44 |

At the comparison endpoint Paper had 73 fills (24 Qualified / 49 Research), $62,633.70 turnover, $35.6173 fees and $-0.003804 estimated funding. The archive stopped four fills later: 77 fills (27 Qualified / 50 Research), $62,770.07 turnover, $35.6787 fees, $-0.003804 funding, $-45.1617 pooled P&L, and $45.30 maximum drawdown (0.10219%). These startup costs remain in totals. The first eight opening-inventory adjustments used $60,099.00 turnover, cost $34.4707 in fees and realized $-297.5509.

| Account | Stop equity | Fees | Funding | Turnover | Max drawdown |
|---|---:|---:|---:|---:|---:|
| Alex | $6,524.21 | $8.2649 | $+0.001223 | $18,366.48 | $10.28 / 0.15732% |
| Clear Pond | $32,872.95 | $20.8101 | $+0.000000 | $29,728.71 | $33.19 / 0.10088% |
| Jeremy | $4,886.73 | $6.6037 | $-0.005027 | $14,674.89 | $17.86 / 0.36422% |

Holds: 86 below rebalance threshold, 83 opposite account direction, 72 forecast unavailable, 4 gross capacity exhausted. Skips: 24 below size precision and 2 below the executable $10 floor. These remained holds/skips; no fills were fabricated. Per-coin costs and source ledger rows are in the retained parquet/SQLite evidence.

## Archive and next opening

Round 52's archive is retained at `C:/DATASTORE/hyperliquid/_paper_archives/20261003-paper-round-53`: 429 files / 462,126,982 bytes, manifest SHA-256 `d44224e201b64f093fa01e2ac2ac096851240f17c5394d9ae4b0033c18afe741`. Archived SQLite integrity check: `ok`; ledger SHA-256 `82ac9aee5ec4b9bc3f09e63c1ea8f637c269a451733f55c6255cbb0fde7dffaf`.

Round 53 was accepted and is running from **2026-10-03T03:36:23.680550575+00:00**, with a one-hour due time of **2026-10-03T04:36:23.680550+00:00** (2026-10-02 21:36:23 PDT). Fresh opening equity was **$44,284.95** across 10 inherited positions. All three source `frontendOpenOrders` reads were empty. Independent opening and first-cycle checks passed with zero opening fills, fees, funding, transfers and P&L. The loose Paper policy `88cd1a6a008df9b9`, strict Powder policy, five model families, truthful labels and 5m/one-bar 70/15/15 recipe were retained. No evidence supported a model retune after the unscored comparison.

Latest health passed at 2026-10-03T04:02:30.330847+00:00 (portfolio 2026-10-03T04:02:12.261650+00:00): Paper PID 73932, models PID 14816, coordinator PID 56964 alive; Powder inactive; zero warnings; all four 5m/one-bar forecasts fresh. BTC and ZEC were Qualified; ETH and HYPE Research. Current Paper equity was $44,249.81, total P&L $-35.14, fees $34.6418, and funding $+0.000000. At this sample, the active ledger held 20 fills and $60,510.22 turnover with zero funding/transfers. A health read at 03:55:02 UTC caught the scheduled refresh boundary and rejected stale forecasts; the next two reads after fresh model cycles passed.

## Changes, checks, limits

No application source, shared configuration, Atlas symbol configuration, model weights or Paper policy changed. Current exploratory admission remains enabled; Powder remains disconnected under the strict policy. No software test suite was run because there was no source/config change. Native cadence, archive, SQLite, opening, first-cycle, maintenance, health and continuous-cycle checks passed.

The only new project files are machine-local operating-artifact staging copies under `C:/dev/ducketz/artifacts/analysis/hyperliquid-paper-improvement/20261003-round52-53`. The publisher manifest will record each exact operation and SHA. These outputs may be used as Atlas reference material; public publication does not mean Scout adopted them or that runtime deployment occurred. Local main is 17 commits behind the verified public base and was not merged or changed. Existing unrelated untracked analysis artifacts are preserved.

Round 53 is still in progress and cannot be ranked yet. Future-round comparison remains subject to due-window timing, common marks, external flows and the native evidence rules. See the machine-readable record, project-file inventory, and archived ledger/evidence named in the inventory for exact hashes.
