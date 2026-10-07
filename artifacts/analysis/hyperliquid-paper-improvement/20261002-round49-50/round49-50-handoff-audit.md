# Hyperliquid Paper Improvement — Round 49 closeout / Round 50 handoff

- Completed artifact bundle: 2026-10-02T22:13:11.588212Z; producer **hyperliquid-paper-improvement**, actor **Atlas / pc-original**.
- Round49 was a one-hour evaluation seeded 2026-10-02T19:36:12.740584135+00:00, due 2026-10-02T20:36:12.740584+00:00. Native result: **UNSCORED (`late_unscored`)**. Paper endpoint was 2026-10-02T21:25:44.340739+00:00 (2971.600s after due); latest actual account read was 2026-10-02T21:26:07.073472+00:00 (2994.333s late). The five-minute timing gate failed, so cadence retained one hour and consumed this assessment once after Round50 acceptance.
- Descriptive after-cost comparison: common-mark Paper $44,109.740529, actual $44,204.263018, edge Paper-minus-actual $-94.522489. Returns: Paper -0.064221%, actual 0.149931%, excess -0.214152 percentage points. Baseline reconciliation, complete zero-flow histories, comparable performance, and 23.015s skew passed. These are not a score because of lateness.

## Per-account comparative endpoint

| Account | Paper common mark | Actual common mark | Paper minus actual | Paper return | Actual return | Excess |
|---|---:|---:|---:|---:|---:|---:|
| alex | $6,903.18 | $7,061.00 | $-157.82 | -0.0473% | 2.2377% | -2.2851 pp |
| clearpond | $32,611.23 | $32,653.33 | $-42.11 | -0.0412% | 0.0879% | -0.1291 pp |
| jeremy | $4,595.33 | $4,489.93 | $105.40 | -0.2525% | -2.5403% | 2.2879 pp |

## Round49 stopped ledger

- Archived ledger is `C:\DATASTORE\hyperliquid\_paper_archives\20261002-paper-round-50`: 444 files / 481,918,099 bytes; manifest SHA-256 `3d71c2e9010872fa6db1b14f5b7e08157f46ea53f8962f95572d13b5d51c61bb`. Native archive verification passed at 21:32:18.860078Z; read-only SQLite integrity check returned `ok` at 2026-10-02T22:06:41.708202Z.
- Stop time: 2026-10-02T21:30:31.540219+00:00. Activity: 346 cycles, 366 decisions, 66 fills, $61,102.41304 turnover, $34.856816 fees, $-0.004201 funding, 0 transfers. Pooled ending equity $44,109.138715; total P&L $-28.947613; realized P&L $-192.031578; max drawdown $28.994730 (0.065691%).
- Forecast-linked fills: 20 Qualified and 46 Research (65 signal rebalances and 1 risk-cap fill). At comparison endpoint the forecast witness recorded 88 valid forecasts, 12 invalid consumed decisions, 69 Qualified decisions, and 22 valid Qualified forecasts under the v2 rule.

| Account | Ending equity | Total P&L | Fees | Funding | Turnover | Fills (Q/R) | Max drawdown |
|---|---:|---:|---:|---:|---:|---:|---:|
| alex | $6,903.21 | $-3.24 | $8.0127 | $0.00095 | $17,806.00 | 22 (17/5) | $9.58 (0.1386%) |
| clearpond | $32,611.17 | $-13.50 | $20.6100 | $0.00000 | $29,442.92 | 5 (1/4) | $13.51 (0.0414%) |
| jeremy | $4,594.76 | $-12.21 | $6.2341 | $-0.00515 | $13,853.49 | 39 (2/37) | $12.25 (0.2660%) |

Holds: below rebalance 104, forecast unavailable 78, opposing account direction 66, gross capacity exhausted 4. Skips: below quantity precision 28, below executable $10 floor 20.

By coin:
| Coin | Fills | Turnover | Fees | Realized P&L |
|---|---:|---:|---:|---:|
| BTC | 20 | $22,015.54 | $15.1907 | $8.44 |
| ETH | 19 | $3,133.33 | $2.0796 | $-0.21 |
| HYPE | 11 | $18,397.47 | $9.3623 | $-118.09 |
| ZEC | 16 | $17,556.07 | $8.2242 | $-82.17 |

Round49 initial inventory adjustment: 8 fills, $59,142.04834 turnover, $33.971791 fees, $-186.980745 realized P&L. These costs stay in the round totals.

## Round50 accepted and running

- One-hour seed 2026-10-02T21:37:46.860747337+00:00; due 2026-10-02T22:37:46.860747+00:00; opening equity $44,205.680399 across Alex $7,040.24, Clear Pond $32,660.05, Jeremy $4,505.39. Ten signed positions reconciled to 13 retained public reads; all three source-order reads were empty. Opening fills/fees/funding/transfers/decisions/P&L were zero; independent opening verification passed.
- First committed cycle passed: 8 inventory-adjustment fills, $58,938.8846 turnover, $33.88852735 fees and $-162.27009 realized P&L, retained in the active ledger.
- Exploratory policy `88cd1a6a008df9b9` stays in place: Research and Qualified forecasts admitted, zero entry/exit/rebalance bands, executable $10 floor, normal book/cash/precision/fees/stops/cooldowns/exposure caps retained. Truthful labels, five families and 5m/one-bar 70/15/15 recipe remain; no retune was justified by an unscored ending round. Powder stays inactive under its separate strict policy.
- Latest read-only health passed at 2026-10-02T22:10:56.634274+00:00: Paper, Models and coordinator identities passed; Powder was inactive, warnings were empty, and all four configured 5m/h1 forecast slots passed. Current labels at that read: BTC Qualified, ETH Research, HYPE Qualified, ZEC Research.
- A health sample at 22:10:01Z reported Paper partial due to runtime source errors. Saved status at 22:10:34Z had `last_error=null` and `errors={}`; subsequent read-only health passed at 22:10:56Z without restart. Cause remains unknown.

## Checks, publication, and next wake

- Pinned cross-pc-v2 installation, native comparison/cadence, archive preservation, SQLite integrity, opening mirror, first cycle, accepted maintenance, cadence advance and latest health are recorded in the JSON receipt with timestamps. No application tests ran because no source/configuration changed.
- No shared source queue item was created. The running checkout stayed on `main` at `99532621db0d42dfe8c655300cc93abda81240d5`; project additions below `artifacts/analysis/hyperliquid-paper-improvement/20261002-round49-50` are operating-artifact staging copies. Atlas’s 11-stock set and symbol settings are untouched.
- Public publication is completion `20261002T220641Z-hlp50-round4950` via the pinned artifact publisher. The local publisher receipt records exact source and destination hashes, stable-byte and `.env` private-value checks, and the verified remote main SHA. This publication does not grant Scout adoption or indicate deployment.
- Hyperliquid Paper Improvement stayed PAUSED throughout work. Final app handoff is full PAUSED then ACTIVE, hourly interval 1, preserving prompt, gpt-6-luna/max and project. The exact request time and estimated next wake are written to the adjacent local `schedule-handoff.json` immediately before those final calls. If its hourly wake occurs after Round50’s 22:37:46.860747Z due plus five minutes, record the next endpoint UNSCORED.

Limitations: external account reads are sequential rather than atomic; this native sample’s skew was within limits. A short overlapping-forecast round cannot establish profitability. Initial inventory costs are itemized but remain included. No runtime deployment occurred.
